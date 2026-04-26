import argparse
import json
import os
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("VOCAB_DB_PATH", REPO_ROOT / "data" / "vocabulary.db")).expanduser()


def get_excluded_words(pos_filter: str | None, limit: int | None) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        query = "SELECT id, word, pos, cefr FROM words WHERE excluded = 1"
        params: list = []

        if pos_filter:
            query += " AND pos = ?"
            params.append(pos_filter)

        query += " ORDER BY RANDOM()"

        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        rows = conn.execute(query, params).fetchall()

    return [dict(row) for row in rows]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pos", default=None, help="filter by part of speech")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    print(json.dumps(get_excluded_words(args.pos, args.limit), ensure_ascii=False, indent=2))
