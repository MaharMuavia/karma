"""SQLite persistence for the Karma reputation registry.

A single-file database keeps deployment trivial on free tiers (no external
service to provision). Connections are opened per request with a short busy
timeout so concurrent writers serialize instead of erroring.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

# On most free tiers the working directory is writable and ephemeral; allow an
# override so a mounted persistent disk can be used when available.
DB_PATH = os.environ.get("KARMA_DB_PATH", "karma.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id           TEXT PRIMARY KEY,
    display_name TEXT,
    first_seen   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    reviewer_id  TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    rating       INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    outcome      TEXT NOT NULL CHECK (outcome IN ('succeeded','failed','partial')),
    task_summary TEXT NOT NULL DEFAULT '',
    evidence_url TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_reviews_subject  ON reviews(subject_id);
CREATE INDEX IF NOT EXISTS idx_reviews_reviewer ON reviews(reviewer_id);
"""


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection with row access by column name."""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create tables and indexes if they do not already exist."""
    with get_conn() as conn:
        conn.executescript(_SCHEMA)


def upsert_agent(conn: sqlite3.Connection, agent_id: str, display_name: str | None) -> None:
    """Ensure an agent row exists, filling in a display name if newly provided."""
    conn.execute(
        "INSERT INTO agents (id, display_name) VALUES (?, ?) "
        "ON CONFLICT(id) DO UPDATE SET display_name = COALESCE(excluded.display_name, display_name)",
        (agent_id, display_name),
    )
