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


def init():
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
    """Run multiple (sql, params) pairs in one transaction."""
    if USE_TURSO:
        for sql, params in statements:
            _libsql_conn.execute(sql, params)
        _libsql_conn.commit()
        _libsql_conn.sync()
    else:
        with _sqlite_conn() as conn:
            for sql, params in statements:
                conn.execute(sql, params)
