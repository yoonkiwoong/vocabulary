import os
import sqlite3
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LOCAL_DB = str(_REPO_ROOT / "data" / "vocabulary.db")
_TURSO_URL = os.environ.get("TURSO_DATABASE_URL", "")
_TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")
USE_TURSO = bool(_TURSO_URL and _TURSO_TOKEN)

_client = None

_DDL = [
    """CREATE TABLE IF NOT EXISTS words (
        id INTEGER PRIMARY KEY, word TEXT NOT NULL, pos TEXT NOT NULL,
        cefr TEXT, excluded INTEGER NOT NULL DEFAULT 0,
        definition TEXT, UNIQUE(word, pos)
    )""",
    """CREATE TABLE IF NOT EXISTS schedule (
        word_id INTEGER PRIMARY KEY REFERENCES words(id),
        stability REAL, difficulty REAL, state TEXT DEFAULT 'new',
        step INTEGER, last_review TEXT, reps INTEGER DEFAULT 0,
        lapses INTEGER DEFAULT 0, due_at TEXT NOT NULL, learned_at TEXT DEFAULT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT, word_id INTEGER REFERENCES words(id),
        reviewed_at TEXT NOT NULL,
        rating TEXT NOT NULL CHECK(rating IN ('again', 'good'))
    )""",
]


def _rs_to_dicts(rs) -> list[dict]:
    return [dict(zip(rs.columns, row)) for row in rs.rows]


def _chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def init() -> None:
    global _client
    if not USE_TURSO:
        return
    import libsql_client as lc
    http_url = _TURSO_URL.replace("libsql://", "https://", 1)
    _client = lc.create_client_sync(url=http_url, auth_token=_TURSO_TOKEN)

    _client.batch([lc.Statement(ddl) for ddl in _DDL])

    try:
        _client.execute("ALTER TABLE words ADD COLUMN definition TEXT")
    except Exception:
        pass  # column already exists

    rs = _client.execute("SELECT COUNT(*) FROM words")
    if rs.rows[0][0] == 0:
        _seed()


def _seed() -> None:
    import libsql_client as lc

    local = sqlite3.connect(_LOCAL_DB)
    local.row_factory = sqlite3.Row
    words    = local.execute("SELECT * FROM words").fetchall()
    schedule = local.execute("SELECT * FROM schedule").fetchall()
    reviews  = local.execute("SELECT * FROM reviews").fetchall()
    local.close()

    for chunk in _chunked(words, 200):
        _client.batch([
            lc.Statement(
                "INSERT OR IGNORE INTO words (id, word, pos, cefr, excluded) VALUES (?, ?, ?, ?, ?)",
                [r["id"], r["word"], r["pos"], r["cefr"], r["excluded"]],
            )
            for r in chunk
        ])

    for chunk in _chunked(schedule, 200):
        _client.batch([
            lc.Statement(
                """INSERT OR IGNORE INTO schedule
                   (word_id, stability, difficulty, state, step,
                    last_review, reps, lapses, due_at, learned_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [r["word_id"], r["stability"], r["difficulty"], r["state"],
                 r["step"], r["last_review"], r["reps"], r["lapses"],
                 r["due_at"], r["learned_at"]],
            )
            for r in chunk
        ])

    for chunk in _chunked(reviews, 200):
        _client.batch([
            lc.Statement(
                "INSERT OR IGNORE INTO reviews (id, word_id, reviewed_at, rating) VALUES (?, ?, ?, ?)",
                [r["id"], r["word_id"], r["reviewed_at"], r["rating"]],
            )
            for r in chunk
        ])

    print(f"[db] seeded {len(words)} words, {len(schedule)} schedule, {len(reviews)} reviews")


def _dict_factory(cursor, row):
    return {d[0]: v for d, v in zip(cursor.description, row)}


def _sqlite_conn():
    conn = sqlite3.connect(_LOCAL_DB)
    conn.row_factory = _dict_factory
    return conn


def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    if USE_TURSO:
        import libsql_client as lc
        rs = _client.execute(lc.Statement(sql, list(params)) if params else sql)
        return _rs_to_dicts(rs)
    with _sqlite_conn() as conn:
        return conn.execute(sql, params).fetchall()


def fetch_one(sql: str, params: tuple = ()) -> dict | None:
    if USE_TURSO:
        import libsql_client as lc
        rs = _client.execute(lc.Statement(sql, list(params)) if params else sql)
        rows = _rs_to_dicts(rs)
        return rows[0] if rows else None
    with _sqlite_conn() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> None:
    if USE_TURSO:
        import libsql_client as lc
        _client.execute(lc.Statement(sql, list(params)) if params else sql)
    else:
        with _sqlite_conn() as conn:
            conn.execute(sql, params)


def execute_many(statements: list[tuple]) -> None:
    if USE_TURSO:
        import libsql_client as lc
        _client.batch([lc.Statement(sql, list(params)) for sql, params in statements])
    else:
        with _sqlite_conn() as conn:
            for sql, params in statements:
                conn.execute(sql, params)
