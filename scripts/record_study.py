import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from fsrs import Card, Rating, Scheduler
    try:
        from fsrs import State
    except ImportError:
        State = None
except ImportError as exc:
    Card = Rating = Scheduler = State = None
    FSRS_IMPORT_ERROR = exc
else:
    FSRS_IMPORT_ERROR = None

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("VOCAB_DB_PATH", REPO_ROOT / "data" / "vocabulary.db")).expanduser()

RATING_MAP = {
    "again": None,
    "good": None,
}


def _init_rating_map() -> None:
    if Rating is not None:
        RATING_MAP["again"] = Rating.Again
        RATING_MAP["good"] = Rating.Good


_init_rating_map()


def require_fsrs() -> None:
    if FSRS_IMPORT_ERROR is not None:
        raise RuntimeError(
            "fsrs is required. Install with `pip install -r requirements.txt`."
        ) from FSRS_IMPORT_ERROR


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return ensure_aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def card_from_row(row: sqlite3.Row) -> "Card":
    require_fsrs()
    card = Card()
    if row["stability"] is None:
        return card

    card.stability = row["stability"]
    card.difficulty = row["difficulty"]
    card.reps = row["reps"]
    card.lapses = row["lapses"]

    if row["last_review"]:
        card.last_review = parse_dt(row["last_review"])

    if row["due_at"]:
        card.due = parse_dt(row["due_at"])

    if row["state"] is not None and State is not None:
        try:
            card.state = State[row["state"]]
        except KeyError:
            pass

    if row["step"] is not None:
        card.step = row["step"]

    return card


def record(word_id: int, rating_str: str) -> None:
    require_fsrs()
    if RATING_MAP.get(rating_str) is None:
        raise ValueError(f"rating must be one of {tuple(RATING_MAP)}")
    if not DB_PATH.exists():
        raise FileNotFoundError(f"database not found: {DB_PATH}")

    reviewed_at = utc_now()
    reviewed_at_iso = reviewed_at.isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT stability, difficulty, state, step, last_review, reps, lapses, due_at, learned_at "
            "FROM schedule WHERE word_id = ?",
            (word_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"word_id {word_id} not found in schedule")

        card = card_from_row(row)
        scheduler = Scheduler()
        result = scheduler.review_card(card, RATING_MAP[rating_str])
        updated = result[0] if isinstance(result, tuple) else result

        due_iso = ensure_aware_utc(updated.due).isoformat()
        next_reps = getattr(updated, "reps", None)
        if next_reps is None or next_reps == row["reps"]:
            next_reps = (row["reps"] or 0) + 1

        next_lapses = getattr(updated, "lapses", None)
        if next_lapses is None:
            next_lapses = row["lapses"] or 0
        if rating_str == "again" and next_lapses == row["lapses"]:
            next_lapses += 1

        conn.execute(
            """
            UPDATE schedule SET
                stability   = ?,
                difficulty  = ?,
                state       = ?,
                step        = ?,
                last_review = ?,
                reps        = ?,
                lapses      = ?,
                due_at      = ?,
                learned_at  = COALESCE(learned_at, ?)
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
                word_id,
            ),
        )
        conn.execute(
            "INSERT INTO reviews (word_id, reviewed_at, rating) VALUES (?, ?, ?)",
            (word_id, reviewed_at_iso, rating_str),
        )

    print(f"Recorded: word_id={word_id} rating={rating_str} next_due={due_iso}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--word-id", type=int, required=True)
    parser.add_argument("--rating", choices=["again", "good"], required=True)
    args = parser.parse_args()

    try:
        record(args.word_id, args.rating)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
