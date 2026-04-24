import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DB = REPO_ROOT / "data" / "oxford_words.db"
DST_DB = REPO_ROOT / "data" / "vocabulary.db"

CEFR_ORDER = ["A1", "A2", "B1", "B2", "C1"]


def min_cefr(levels: list[str]) -> str:
    valid = [c for c in levels if c in CEFR_ORDER]
    if not valid:
        return levels[0]
    return min(valid, key=CEFR_ORDER.index)


def main() -> None:
    src = sqlite3.connect(SRC_DB)
    dst = sqlite3.connect(DST_DB)

    rows = src.execute(
        "SELECT base_word, pos, cefr FROM words ORDER BY base_word, pos"
    ).fetchall()
    src.close()

    # Group by (word, pos), resolve CEFR conflicts with lowest level
    grouped: dict[tuple[str, str], list[str]] = {}
    for word, pos, cefr in rows:
        key = (word, pos)
        grouped.setdefault(key, []).append(cefr)

    now = datetime.now(timezone.utc).isoformat()
    words_inserted = 0
    schedule_inserted = 0

    with dst:
        for (word, pos), cefr_list in grouped.items():
            cefr = min_cefr(cefr_list)
            cur = dst.execute(
                "INSERT OR IGNORE INTO words (word, pos, cefr) VALUES (?, ?, ?)",
                (word, pos, cefr),
            )
            if cur.rowcount:
                words_inserted += 1
                word_id = cur.lastrowid
                dst.execute(
                    "INSERT INTO schedule (word_id, due_at) VALUES (?, ?)",
                    (word_id, now),
                )
                schedule_inserted += 1

    dst.close()

    print(f"words inserted   : {words_inserted}")
    print(f"schedule inserted: {schedule_inserted}")
    print(f"skipped (dup)    : {len(grouped) - words_inserted}")


if __name__ == "__main__":
    main()
