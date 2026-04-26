import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("VOCAB_DB_PATH", REPO_ROOT / "data" / "vocabulary.db")).expanduser()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_due_words(limit: int) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        # Review cards that are due before introducing untouched cards.
        rows = conn.execute(
            """
            SELECT
                w.id,
                w.word,
                w.pos,
                w.cefr,
                s.due_at,
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
            (utc_now_iso(), limit),
        ).fetchall()

    payload = []
    for row in rows:
        item = dict(row)
        item["is_new"] = bool(item["is_new"])
        payload.append(item)
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    print(json.dumps(get_due_words(args.limit), ensure_ascii=False, indent=2))
