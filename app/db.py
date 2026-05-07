import os
import sqlite3
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LOCAL_DB = str(_REPO_ROOT / "data" / "vocabulary.db")
_TURSO_URL = os.environ.get("TURSO_DATABASE_URL", "")
_TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")
USE_TURSO = bool(_TURSO_URL and _TURSO_TOKEN)

_libsql_conn = None


def _dict_factory(cursor, row):
    return {d[0]: v for d, v in zip(cursor.description, row)}


def _create_tables(conn) -> None:
    for ddl in [
        """CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY,
            word TEXT NOT NULL,
            pos TEXT NOT NULL,
            cefr TEXT,
            excluded INTEGER NOT NULL DEFAULT 0,
            UNIQUE(word, pos)
        )""",
        """CREATE TABLE IF NOT EXISTS schedule (
            word_id INTEGER PRIMARY KEY REFERENCES words(id),
            stability REAL,
            difficulty REAL,
            state TEXT DEFAULT 'new',
            step INTEGER,
            last_review TEXT,
            reps INTEGER DEFAULT 0,
            lapses INTEGER DEFAULT 0,
            due_at TEXT NOT NULL,
            learned_at TEXT DEFAULT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word_id INTEGER REFERENCES words(id),
            reviewed_at TEXT NOT NULL,
            rating TEXT NOT NULL CHECK(rating IN ('again', 'good'))
        )""",
    ]:
        conn.execute(ddl)
    conn.commit()


def _seed_from_local(conn) -> None:
    local = sqlite3.connect(_LOCAL_DB)
    local.row_factory = sqlite3.Row

    words = local.execute("SELECT * FROM words").fetchall()
    schedule = local.execute("SELECT * FROM schedule").fetchall()
    reviews = local.execute("SELECT * FROM reviews").fetchall()
    local.close()

    conn.executemany(
        "INSERT OR IGNORE INTO words (id, word, pos, cefr, excluded) VALUES (?,?,?,?,?)",
        [(r["id"], r["word"], r["pos"], r["cefr"], r["excluded"]) for r in words],
    )
    conn.commit()

    conn.executemany(
        """INSERT OR IGNORE INTO schedule
           (word_id, stability, difficulty, state, step, last_review, reps, lapses, due_at, learned_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [(r["word_id"], r["stability"], r["difficulty"], r["state"], r["step"],
          r["last_review"], r["reps"], r["lapses"], r["due_at"], r["learned_at"])
         for r in schedule],
    )
    conn.commit()

    conn.executemany(
        "INSERT OR IGNORE INTO reviews (id, word_id, reviewed_at, rating) VALUES (?,?,?,?)",
        [(r["id"], r["word_id"], r["reviewed_at"], r["rating"]) for r in reviews],
    )
    conn.commit()
    conn.sync()
    print(f"[db] seeded {len(words)} words, {len(schedule)} schedule, {len(reviews)} reviews")


def init() -> None:
    global _libsql_conn
    if not USE_TURSO:
        return
    import libsql_experimental as libsql
    _libsql_conn = libsql.connect(
        database="/tmp/vocabulary_replica.db",
        sync_url=_TURSO_URL,
        auth_token=_TURSO_TOKEN,
    )
    _libsql_conn.sync()
    _libsql_conn.row_factory = _dict_factory

    _create_tables(_libsql_conn)

    count = _libsql_conn.execute("SELECT COUNT(*) FROM words").fetchone()
    if (count[0] if isinstance(count, tuple) else list(count.values())[0]) == 0:
        print("[db] Turso is empty — seeding from local vocabulary.db")
        _seed_from_local(_libsql_conn)


def _sqlite_conn():
    conn = sqlite3.connect(_LOCAL_DB)
    conn.row_factory = _dict_factory
    return conn


def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    if USE_TURSO:
        return _libsql_conn.execute(sql, params).fetchall()
    with _sqlite_conn() as conn:
        return conn.execute(sql, params).fetchall()


def fetch_one(sql: str, params: tuple = ()) -> dict | None:
    if USE_TURSO:
        return _libsql_conn.execute(sql, params).fetchone()
    with _sqlite_conn() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> None:
    if USE_TURSO:
        _libsql_conn.execute(sql, params)
        _libsql_conn.commit()
        _libsql_conn.sync()
    else:
        with _sqlite_conn() as conn:
            conn.execute(sql, params)


def execute_many(statements: list[tuple]) -> None:
    if USE_TURSO:
        for sql, params in statements:
            _libsql_conn.execute(sql, params)
        _libsql_conn.commit()
        _libsql_conn.sync()
    else:
        with _sqlite_conn() as conn:
            for sql, params in statements:
                conn.execute(sql, params)
