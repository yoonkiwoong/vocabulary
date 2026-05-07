import json
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import db

try:
    from fsrs import Card, Rating, Scheduler, State
    _FSRS_OK = True
except ImportError:
    _FSRS_OK = False


# ── lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    yield


app = FastAPI(lifespan=lifespan)


# ── helpers ───────────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return _ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _card_from_row(row: dict) -> "Card":
    card = Card()
    if row["stability"] is None:
        return card
    card.stability = row["stability"]
    card.difficulty = row["difficulty"]
    card.reps = row["reps"]
    card.lapses = row["lapses"]
    if row["last_review"]:
        card.last_review = _parse_dt(row["last_review"])
    if row["due_at"]:
        card.due = _parse_dt(row["due_at"])
    if row["state"] is not None:
        try:
            card.state = State[row["state"]]
        except (KeyError, AttributeError):
            pass
    if row["step"] is not None:
        card.step = row["step"]
    return card


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/words")
def get_words(limit: int = 20):
    now = _utc_now().isoformat()
    rows = db.fetch_all(
        """
        SELECT w.id, w.word, w.pos, w.cefr,
               s.due_at,
               s.reps AS repetitions,
               CASE WHEN s.learned_at IS NULL THEN 1 ELSE 0 END AS is_new
        FROM words AS w
        JOIN schedule AS s ON w.id = s.word_id
        WHERE (s.learned_at IS NULL OR s.due_at <= ?)
          AND w.excluded = 0
        ORDER BY
            CASE WHEN s.learned_at IS NULL THEN 1 ELSE 0 END,
            RANDOM()
        LIMIT ?
        """,
        (now, limit),
    )
    return [dict(r, is_new=bool(r["is_new"])) for r in rows]


class ReviewRequest(BaseModel):
    word_id: int
    rating: str  # "again" | "good"


@app.post("/api/review")
def post_review(req: ReviewRequest):
    if req.rating not in ("again", "good"):
        raise HTTPException(400, "rating must be 'again' or 'good'")
    if not _FSRS_OK:
        raise HTTPException(500, "fsrs not available")

    rating_obj = Rating.Again if req.rating == "again" else Rating.Good

    row = db.fetch_one(
        "SELECT stability, difficulty, state, step, last_review, reps, lapses, due_at, learned_at "
        "FROM schedule WHERE word_id = ?",
        (req.word_id,),
    )
    if row is None:
        raise HTTPException(404, f"word_id {req.word_id} not found")

    card = _card_from_row(row)
    scheduler = Scheduler()
    result = scheduler.review_card(card, rating_obj)
    updated = result[0] if isinstance(result, tuple) else result

    reviewed_at = _utc_now()
    reviewed_at_iso = reviewed_at.isoformat()
    due_iso = _ensure_utc(updated.due).isoformat()

    next_reps = getattr(updated, "reps", None)
    if next_reps is None or next_reps == row["reps"]:
        next_reps = (row["reps"] or 0) + 1

    next_lapses = getattr(updated, "lapses", None)
    if next_lapses is None:
        next_lapses = row["lapses"] or 0
    if req.rating == "again" and next_lapses == row["lapses"]:
        next_lapses += 1

    db.execute_many([
        (
            """
            UPDATE schedule SET
                stability = ?, difficulty = ?, state = ?, step = ?,
                last_review = ?, reps = ?, lapses = ?, due_at = ?,
                learned_at = COALESCE(learned_at, ?)
            WHERE word_id = ?
            """,
            (
                updated.stability,
                updated.difficulty,
                getattr(getattr(updated, "state", None), "name", None),
                getattr(updated, "step", None),
                reviewed_at_iso,
                next_reps,
                next_lapses,
                due_iso,
                reviewed_at_iso,
                req.word_id,
            ),
        ),
        (
            "INSERT INTO reviews (word_id, reviewed_at, rating) VALUES (?, ?, ?)",
            (req.word_id, reviewed_at_iso, req.rating),
        ),
    ])

    return {"ok": True, "next_due": due_iso}


@app.get("/api/hint/{word_id}")
def get_hint(word_id: int):
    row = db.fetch_one("SELECT word, definition FROM words WHERE id = ?", (word_id,))
    if row is None:
        raise HTTPException(404, f"word_id {word_id} not found")

    if row["definition"]:
        return {"definition": row["definition"]}

    word = row["word"]
    definition = _fetch_definition(word)
    if definition:
        db.execute("UPDATE words SET definition = ? WHERE id = ?", (definition, word_id))
    return {"definition": definition}


def _fetch_definition(word: str) -> Optional[str]:
    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vocabulary-app/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        return data[0]["meanings"][0]["definitions"][0]["definition"]
    except Exception:
        return None


# ── UI ────────────────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vocabulary</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0f0f1a;
    color: #e8e8f0;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    height: 100dvh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  #progress-bar-wrap {
    height: 3px;
    background: #1e1e2e;
    flex-shrink: 0;
  }
  #progress-bar {
    height: 100%;
    background: rgba(255,255,255,0.28);
    transition: width 0.35s ease;
    width: 0%;
  }
  #bottom-area {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    padding: 0 24px calc(28px + env(safe-area-inset-bottom));
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  #definition-wrap {
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
    max-height: 30vh;
    overflow-y: auto;
  }
  #card-area {
    flex: 1;
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding: 12vh 24px 0;
    cursor: pointer;
  }
  .card {
    text-align: center;
    max-width: 480px;
    width: 100%;
  }
  .word {
    font-size: clamp(2rem, 8vw, 3.5rem);
    font-weight: 700;
    letter-spacing: -0.02em;
    margin-bottom: 20px;
    color: #f0f0ff;
  }
  .badges {
    display: flex;
    justify-content: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 12px;
  }
  .badge {
    padding: 4px 14px;
    border-radius: 20px;
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .b-pos    { background: #1e2d4a; color: #7eb8f7; }
  .b-cefr-a { background: #1a2e1e; color: #6fcf97; }
  .b-cefr-b { background: #2e2614; color: #f5b942; }
  .b-cefr-c { background: #2e1a1a; color: #f78e7e; }
  .b-new    { background: #2a1a2e; color: #e07ab8; border: 1px solid #4a2a4e; }
  .b-reps   { background: #1e1a2e; color: #9b89f5; border: 1px solid #3a2e6a; }
  .buttons {
    display: flex;
    flex-direction: column;
    gap: 10px;
    width: 100%;
    max-width: 320px;
    margin: 0 auto;
  }
  .btn {
    padding: 14px 20px;
    border: none;
    border-radius: 10px;
    font-size: 1rem;
    font-weight: 600;
    cursor: pointer;
    transition: transform 0.1s, opacity 0.1s;
    letter-spacing: 0.02em;
    width: 100%;
  }
  .btn:active { transform: scale(0.97); }
  .btn:disabled { opacity: 0.5; cursor: default; }
  .btn-stop  { background: #1a1a2a; color: #7878a0; border: 1px solid #2a2a44; width: 100%; padding: 10px; font-size: 0.85rem; border-radius: 10px; }
  .btn-again { background: #c0392b; color: #fff; flex: 1; width: auto; padding: 22px 20px; font-size: 1.1rem; }
  .btn-hint  { background: #1a2238; color: #7eb8f7; border: 1px solid #2a3a5e; }
  .btn-good  { background: #27ae60; color: #fff; flex: 1; width: auto; padding: 22px 20px; font-size: 1.1rem; }
  .main-buttons { display: flex; gap: 12px; width: 100%; }
  .definition {
    padding: 12px 16px;
    background: #13131f;
    border-left: 3px solid rgba(255,255,255,0.28);
    border-radius: 0 8px 8px 0;
    font-size: 0.9rem;
    line-height: 1.6;
    color: #b0b0cc;
    text-align: left;
    display: none;
  }
  .done-title {
    font-size: 1.8rem;
    font-weight: 700;
    margin-bottom: 8px;
  }
  .done-sub {
    color: #555570;
    margin-bottom: 32px;
  }
  .stats {
    display: flex;
    gap: 32px;
    justify-content: center;
    margin-bottom: 36px;
  }
  .stat-num  { font-size: 2.4rem; font-weight: 700; }
  .stat-label{ font-size: 0.8rem; color: #555570; margin-top: 2px; }
  .num-good  { color: #27ae60; }
  .num-again { color: #c0392b; }
  .btn-restart {
    background: #1a1a2e;
    color: #c8c8e0;
    border: 1px solid #2a2a44;
    padding: 13px 36px;
    border-radius: 10px;
    font-size: 1rem;
    font-weight: 600;
    cursor: pointer;
    transition: transform 0.1s;
  }
  .btn-restart:active { transform: scale(0.96); }
  #loading {
    color: #333350;
    font-size: 1rem;
  }
</style>
</head>
<body>
<div id="progress-bar-wrap"><div id="progress-bar"></div></div>
<div id="card-area"><div id="loading">Loading…</div></div>
<div id="bottom-area" style="display:none">
  <div id="definition-wrap">
    <div class="definition" id="definition"></div>
  </div>
  <div id="action-buttons" style="display:none">
    <button class="btn btn-hint" id="hint-btn" onclick="hint()">Hint</button>
    <div class="main-buttons">
      <button class="btn btn-again" onclick="rate('again')">Again</button>
      <button class="btn btn-good"  onclick="rate('good')">Good</button>
    </div>
  </div>
  <button class="btn btn-stop" id="stop-btn" onclick="stop()">Stop</button>
</div>

<script>
let words = [], idx = 0, goodCount = 0, againCount = 0;

const pbar        = document.getElementById('progress-bar');
const bottomArea  = document.getElementById('bottom-area');
const actionBtns  = document.getElementById('action-buttons');
const cardArea    = document.getElementById('card-area');

async function load() {
  const res = await fetch('/api/words');
  words = await res.json();
  idx = goodCount = againCount = 0;
  bottomArea.style.display = 'none';
  resetButtons();
  words.length === 0 ? renderEmpty() : renderCard();
}

function resetButtons() {
  const defEl = document.getElementById('definition');
  defEl.style.display = 'none';
  defEl.textContent = '';
  const hintBtn = document.getElementById('hint-btn');
  hintBtn.disabled = false;
  hintBtn.textContent = 'Hint';
  actionBtns.querySelectorAll('button').forEach(b => b.disabled = false);
  actionBtns.style.display = 'none';
}

function renderCard() {
  const w = words[idx];
  const total = words.length;
  pbar.style.width = (idx / total * 100) + '%';
  const statusBadge = w.is_new
    ? '<span class="badge b-new">NEW</span>'
    : `<span class="badge b-reps">×${w.repetitions}</span>`;
  const cefr = w.cefr || '?';
  const cefrClass = cefr.startsWith('A') ? 'b-cefr-a'
                  : cefr.startsWith('B') ? 'b-cefr-b'
                  : cefr.startsWith('C') ? 'b-cefr-c'
                  : 'b-cefr-a';

  bottomArea.style.display = 'flex';
  cardArea.onclick = reveal;
  cardArea.style.cursor = 'pointer';
  cardArea.innerHTML = `
    <div class="card" id="card">
      <div class="badges">${statusBadge}</div>
      <div class="word">${esc(w.word)}</div>
      <div class="badges">
        <span class="badge b-pos">${esc(w.pos)}</span>
        <span class="badge ${cefrClass}">${esc(cefr)}</span>
      </div>
    </div>`;
}

function reveal() {
  if (actionBtns.style.display === 'none') {
    actionBtns.style.display = 'flex';
    actionBtns.style.flexDirection = 'column';
    actionBtns.style.gap = '10px';
    cardArea.style.cursor = 'default';
    cardArea.onclick = null;
  }
}

function stop() {
  renderDone();
}

async function hint() {
  const w = words[idx];
  const hintBtn = document.getElementById('hint-btn');
  hintBtn.disabled = true;
  hintBtn.textContent = '…';

  try {
    const res = await fetch('/api/hint/' + w.id);
    const data = await res.json();
    const defEl = document.getElementById('definition');
    if (data.definition) {
      defEl.textContent = data.definition;
      defEl.style.display = 'block';
    } else {
      defEl.textContent = 'No definition available.';
      defEl.style.display = 'block';
    }
  } catch (_) {
    hintBtn.disabled = false;
  }
  hintBtn.textContent = 'Hint';
}

async function rate(rating) {
  const w = words[idx];
  actionBtns.querySelectorAll('button').forEach(b => b.disabled = true);
  await fetch('/api/review', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({word_id: w.id, rating}),
  });
  if (rating === 'good') goodCount++; else againCount++;
  idx++;
  resetButtons();
  idx >= words.length ? renderDone() : renderCard();
}

function renderDone() {
  pbar.style.width = '100%';
  bottomArea.style.display = 'none';
  cardArea.onclick = null;
  cardArea.innerHTML = `
    <div class="card">
      <div class="done-title">Session Complete</div>
      <div class="done-sub">${goodCount + againCount} words reviewed</div>
      <div class="stats">
        <div><div class="stat-num num-again">${againCount}</div><div class="stat-label">Again</div></div>
        <div><div class="stat-num num-good">${goodCount}</div><div class="stat-label">Good</div></div>
      </div>
      <button class="btn-restart" onclick="load()">New Session</button>
    </div>`;
}

function renderEmpty() {
  pbar.style.width = '100%';
  bottomArea.style.display = 'none';
  cardArea.onclick = null;
  cardArea.innerHTML = `
    <div class="card">
      <div class="done-title">All caught up!</div>
      <div class="done-sub">No words due right now. Check back later.</div>
    </div>`;
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

load();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def root():
    return _HTML
