"""SQLite: события (аудит) и кеш профилей ревизий по SHA."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    object TEXT NOT NULL,
    result TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS revisions (
    sha TEXT PRIMARY KEY,
    ref TEXT NOT NULL DEFAULT '',
    profile_json TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Event:
    id: int
    ts: str
    action: str
    object: str
    result: str
    details: str


@dataclass(frozen=True)
class Revision:
    sha: str
    ref: str
    profile: dict[str, Any]
    synced_at: str
    last_used_at: str


class Store:
    """Одно соединение на процесс; вызовы короткие, блокировки event loop не заметны."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- события ---------------------------------------------------------
    def event(self, action: str, obj: str, result: str, details: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO events (ts, action, object, result, details) VALUES (?, ?, ?, ?, ?)",
                (_now(), action, obj, result, details),
            )

    def events(self, limit: int = 100) -> list[Event]:
        rows = self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [Event(**dict(r)) for r in rows]

    # --- ревизии ---------------------------------------------------------
    def upsert_revision(self, sha: str, profile: dict[str, Any], ref: str = "") -> None:
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO revisions (sha, ref, profile_json, synced_at, last_used_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(sha) DO UPDATE SET ref=excluded.ref, profile_json=excluded.profile_json,"
                " synced_at=excluded.synced_at, last_used_at=excluded.last_used_at",
                (sha, ref, json.dumps(profile, ensure_ascii=False), now, now),
            )

    def get_revision(self, sha: str) -> Revision | None:
        row = self.conn.execute("SELECT * FROM revisions WHERE sha = ?", (sha,)).fetchone()
        return self._revision(row) if row else None

    def touch_revision(self, sha: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE revisions SET last_used_at = ? WHERE sha = ?", (_now(), sha))

    def delete_revision(self, sha: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM revisions WHERE sha = ?", (sha,))

    def revisions(self) -> list[Revision]:
        rows = self.conn.execute("SELECT * FROM revisions ORDER BY last_used_at DESC").fetchall()
        return [self._revision(r) for r in rows]

    @staticmethod
    def _revision(row: sqlite3.Row) -> Revision:
        return Revision(
            sha=row["sha"],
            ref=row["ref"],
            profile=json.loads(row["profile_json"]),
            synced_at=row["synced_at"],
            last_used_at=row["last_used_at"],
        )
