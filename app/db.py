"""Portable persistence for Karma: SQLite for local dev, Postgres in production.

The store auto-selects its backend from the environment: if ``DATABASE_URL`` (or
``POSTGRES_URL``, which Vercel Postgres injects) is set, it talks to Postgres via
``psycopg``; otherwise it uses a local SQLite file. This lets the same code run
on a serverless host with no persistent disk (Vercel) and on a laptop or a
disk-backed host with zero configuration. All SQL lives here so the two dialects
are reconciled in one place; callers use the typed ``Store`` methods.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
IS_POSTGRES = bool(DATABASE_URL)

# SQLite path (ignored when Postgres is configured). Overridable for tests.
SQLITE_PATH = os.environ.get("KARMA_DB_PATH", "karma.db")

# Timestamp expression that yields an identical "YYYY-MM-DDTHH:MM:SSZ" string
# in both dialects, so API responses look the same regardless of backend.
_TS_SELECT = (
    "to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
    if IS_POSTGRES
    else "created_at"
)

_SQLITE_SCHEMA = """
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

_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id           TEXT PRIMARY KEY,
    display_name TEXT,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS reviews (
    id           BIGSERIAL PRIMARY KEY,
    reviewer_id  TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    rating       INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    outcome      TEXT NOT NULL CHECK (outcome IN ('succeeded','failed','partial')),
    task_summary TEXT NOT NULL DEFAULT '',
    evidence_url TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reviews_subject  ON reviews(subject_id);
CREATE INDEX IF NOT EXISTS idx_reviews_reviewer ON reviews(reviewer_id);
"""


class Store:
    """A tiny data-access layer that works against SQLite or Postgres.

    One instance is shared by the app; each method opens a short-lived
    connection so there is no long-lived state to break under a serverless
    or multi-worker deployment.
    """

    def __init__(self) -> None:
        self.ph = "%s" if IS_POSTGRES else "?"

    # -- connection -----------------------------------------------------

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        if IS_POSTGRES:
            import psycopg
            from psycopg.rows import dict_row

            conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
            # Vercel Postgres pools through pgbouncer (transaction mode), which
            # does not support server-side prepared statements; disable them.
            conn.prepare_threshold = None
        else:
            conn = sqlite3.connect(SQLITE_PATH, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- schema ---------------------------------------------------------

    def init_schema(self) -> None:
        """Create tables and indexes if absent."""
        with self._conn() as conn:
            if IS_POSTGRES:
                # psycopg3 sends one statement per execute(), so run each DDL
                # statement separately rather than as one script.
                for statement in _POSTGRES_SCHEMA.split(";"):
                    if statement.strip():
                        conn.execute(statement)
            else:
                conn.executescript(_SQLITE_SCHEMA)

    def count_reviews(self) -> int:
        """Total number of stored reviews (used to decide whether to seed)."""
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()
            return int(row["n"])

    # -- writes ---------------------------------------------------------

    def upsert_agent(self, conn: Any, agent_id: str, display_name: str | None) -> None:
        """Ensure an agent row exists, filling a display name when newly given."""
        conn.execute(
            f"INSERT INTO agents (id, display_name) VALUES ({self.ph}, {self.ph}) "
            "ON CONFLICT(id) DO UPDATE SET "
            "display_name = COALESCE(EXCLUDED.display_name, agents.display_name)",
            (agent_id, display_name),
        )

    def add_review(
        self,
        reviewer_id: str,
        subject_id: str,
        rating: int,
        outcome: str,
        task_summary: str,
        evidence_url: str | None,
        reviewer_display_name: str | None,
        subject_display_name: str | None,
    ) -> int:
        """Insert a review (creating both agents if new) and return its id."""
        p = self.ph
        with self._conn() as conn:
            self.upsert_agent(conn, reviewer_id, reviewer_display_name)
            self.upsert_agent(conn, subject_id, subject_display_name)
            insert = (
                f"INSERT INTO reviews "
                f"(reviewer_id, subject_id, rating, outcome, task_summary, evidence_url) "
                f"VALUES ({p}, {p}, {p}, {p}, {p}, {p})"
            )
            params = (reviewer_id, subject_id, rating, outcome, task_summary, evidence_url)
            if IS_POSTGRES:
                row = conn.execute(insert + " RETURNING id", params).fetchone()
                return int(row["id"])
            cur = conn.execute(insert, params)
            return int(cur.lastrowid or 0)

    # -- reads ----------------------------------------------------------

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        """Return the agent row (id, display_name) or None."""
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT id, display_name FROM agents WHERE id = {self.ph}", (agent_id,)
            ).fetchone()
            return dict(row) if row is not None else None

    def reviews_for_subject(self, subject_id: str) -> list[dict[str, Any]]:
        """Return (reviewer_id, rating, outcome) for every review of an agent."""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT reviewer_id, rating, outcome FROM reviews WHERE subject_id = {self.ph}",
                (subject_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def received_counts(self, agent_ids: set[str]) -> dict[str, int]:
        """Map each id to how many reviews it has *received* (for weighting)."""
        if not agent_ids:
            return {}
        placeholders = ",".join(self.ph for _ in agent_ids)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT subject_id, COUNT(*) AS n FROM reviews "
                f"WHERE subject_id IN ({placeholders}) GROUP BY subject_id",
                tuple(agent_ids),
            ).fetchall()
            return {str(r["subject_id"]): int(r["n"]) for r in rows}

    def reviews_detail(self, subject_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
        """Return full review rows for an agent, newest first, paginated."""
        p = self.ph
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT id, reviewer_id, subject_id, rating, outcome, task_summary, "
                f"evidence_url, {_TS_SELECT} AS created_at FROM reviews "
                f"WHERE subject_id = {p} ORDER BY id DESC LIMIT {p} OFFSET {p}",
                (subject_id, limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def distinct_subjects(self) -> list[str]:
        """Return every agent id that has received at least one review."""
        with self._conn() as conn:
            rows = conn.execute("SELECT DISTINCT subject_id FROM reviews").fetchall()
            return [str(r["subject_id"]) for r in rows]

    def seed(self, agents: dict[str, str], reviews: list[tuple[str, str, int, str, str]]) -> None:
        """Insert demo agents and reviews in one transaction (used by seeding)."""
        p = self.ph
        with self._conn() as conn:
            for agent_id, name in agents.items():
                self.upsert_agent(conn, agent_id, name)
            for reviewer, subject, rating, outcome, summary in reviews:
                conn.execute(
                    f"INSERT INTO reviews (reviewer_id, subject_id, rating, outcome, task_summary) "
                    f"VALUES ({p}, {p}, {p}, {p}, {p})",
                    (reviewer, subject, rating, outcome, summary),
                )


store = Store()
