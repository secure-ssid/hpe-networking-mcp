"""SQLite-backed per-URL freshness store for RAG source checking.

Tracks cheap HTTP validators (ETag / Last-Modified) and a SHA-256 content
hash per known source URL so `ingestion/check_updates.py` can do a tiered
check: skip unchanged pages using validators, and fall back to a hash
compare only when validators are missing/stale or the site doesn't support
conditional requests. Mirrors the connection/schema conventions of
`src/hpe_networking_mcp/pipeline/state_store.py`.

Lives at data/source_state.sqlite (git-ignored, alongside the other
embedded indexes — rebuildable, never authoritative content itself).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from hpe_networking_mcp._paths import repo_root

ROOT = repo_root()
DEFAULT_DB_PATH = ROOT / "data" / "source_state.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_url_state (
    url             TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    etag            TEXT,
    last_modified   TEXT,
    content_hash    TEXT,
    last_checked_at TEXT,
    last_changed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_url_state_source
    ON source_url_state(source);
"""


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class SourceStateStore:
    """Per-URL freshness state: validators + content hash + timestamps."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def get(self, url: str) -> sqlite3.Row | None:
        """Return the stored state row for a URL, or None if never checked."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM source_url_state WHERE url = ?", (url,)
            ).fetchone()

    def get_all_for_source(self, source: str) -> list[sqlite3.Row]:
        """Return all known URL state rows for one manifest source."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM source_url_state WHERE source = ?", (source,)
            ).fetchall()

    def record_checked(
        self,
        url: str,
        source: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        content_hash: str | None = None,
        changed: bool = False,
    ) -> None:
        """Upsert a URL's state after a check.

        `changed` marks whether this check found different content than the
        previously stored state — callers pass the result of their own
        comparison so this store stays a plain persistence layer, not the
        decision-maker.
        """
        now = _now()
        existing = self.get(url)
        last_changed_at = now if changed else (
            existing["last_changed_at"] if existing else None
        )
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO source_url_state
                    (url, source, etag, last_modified, content_hash,
                     last_checked_at, last_changed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    source=excluded.source,
                    etag=excluded.etag,
                    last_modified=excluded.last_modified,
                    content_hash=excluded.content_hash,
                    last_checked_at=excluded.last_checked_at,
                    last_changed_at=excluded.last_changed_at
                """,
                (url, source, etag, last_modified, content_hash, now, last_changed_at),
            )

    def changed_urls_for_source(self, source: str, since_iso: str) -> list[str]:
        """Return URLs for `source` whose last_changed_at is at/after `since_iso`."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT url FROM source_url_state "
                "WHERE source = ? AND last_changed_at >= ?",
                (source, since_iso),
            ).fetchall()
        return [r["url"] for r in rows]

    def known_url_count(self, source: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM source_url_state WHERE source = ?",
                (source,),
            ).fetchone()
        return int(row["n"]) if row else 0


def get_store(db_path: Path | str = DEFAULT_DB_PATH) -> SourceStateStore:
    return SourceStateStore(db_path)
