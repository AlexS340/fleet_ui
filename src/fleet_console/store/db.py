"""SQLite: события (аудит) и кеш профилей ревизий по SHA."""

from __future__ import annotations

from dataclasses import dataclass, fields
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
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    version_id INTEGER,
    model TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    adopted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    ref TEXT NOT NULL DEFAULT '',
    sha TEXT NOT NULL DEFAULT '',
    worktree TEXT NOT NULL DEFAULT '',
    recipe TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    replicas INTEGER NOT NULL DEFAULT 1,
    inference TEXT NOT NULL DEFAULT 'litellm',
    ttl_minutes INTEGER NOT NULL DEFAULT 120,
    status TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    version_id INTEGER,
    error TEXT NOT NULL DEFAULT '',
    dir TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    ready_at TEXT,
    expires_at TEXT,
    stopped_at TEXT,
    warned INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS session_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    command TEXT NOT NULL,
    log_path TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    returncode INTEGER
);
"""

ACTIVE_SESSION_STATUSES = ("starting", "ready", "stopping", "attention")


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


@dataclass(frozen=True)
class OwnRun:
    """Свой прогон: запущен сервисом (с Ф3) или усыновлён руками по run_id и version_id MLSC."""

    run_id: str
    version_id: int | None
    model: str
    note: str
    adopted_at: str

    def owns_version(self, version_id: int, application: str) -> bool:
        """Версия MLSC этого прогона: по version_id или по run_id в имени приложения (шаблон job_base.yaml fleet)."""
        return version_id == self.version_id or (bool(self.run_id) and self.run_id in application)


@dataclass
class Session:
    """Сессия модели: подъём через MLSC шагами fleet, таймер жизни, гашение."""

    id: str  # он же experiment.run_id для fleet
    recipe: str
    status: str  # starting | ready | stopping | stopped | failed | attention
    created_at: str
    ref: str = ""
    sha: str = ""
    worktree: str = ""
    model: str = ""
    replicas: int = 1
    inference: str = "litellm"
    ttl_minutes: int = 120
    stage: str = ""
    version_id: int | None = None
    error: str = ""
    dir: str = ""
    ready_at: str | None = None
    expires_at: str | None = None
    stopped_at: str | None = None
    warned: int = 0

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_SESSION_STATUSES

    @property
    def holds_gpu(self) -> bool:
        """Версия создана и не погашена: GPU могут быть заняты."""
        return self.version_id is not None and self.status not in {"stopped", "failed"}


@dataclass(frozen=True)
class Stage:
    id: int
    session_id: str
    stage: str
    command: str
    log_path: str
    started_at: str
    finished_at: str | None
    returncode: int | None


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

    # --- свои прогоны ----------------------------------------------------
    def adopt_run(self, run_id: str, version_id: int | None = None, model: str = "", note: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs (run_id, version_id, model, note, adopted_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id) DO UPDATE SET version_id=excluded.version_id, model=excluded.model, note=excluded.note",
                (run_id, version_id, model, note, _now()),
            )

    def get_run(self, run_id: str) -> OwnRun | None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return OwnRun(**dict(row)) if row else None

    def own_runs(self) -> dict[str, OwnRun]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY adopted_at DESC").fetchall()
        return {r["run_id"]: OwnRun(**dict(r)) for r in rows}

    def forget_run(self, run_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))

    # --- сессии моделей --------------------------------------------------
    def create_session(self, s: Session) -> None:
        cols = [f.name for f in fields(Session)]
        with self.conn:
            self.conn.execute(
                f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [getattr(s, c) for c in cols],
            )

    def update_session(self, session_id: str, **values: Any) -> None:
        if not values:
            return
        assignments = ", ".join(f"{k} = ?" for k in values)
        with self.conn:
            self.conn.execute(f"UPDATE sessions SET {assignments} WHERE id = ?", [*values.values(), session_id])

    def get_session(self, session_id: str) -> Session | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return Session(**dict(row)) if row else None

    def sessions(self, active_only: bool = False) -> list[Session]:
        sql = "SELECT * FROM sessions"
        if active_only:
            sql += " WHERE status IN (" + ", ".join("?" * len(ACTIVE_SESSION_STATUSES)) + ")"
        rows = self.conn.execute(sql + " ORDER BY created_at DESC", ACTIVE_SESSION_STATUSES if active_only else ()).fetchall()
        return [Session(**dict(r)) for r in rows]

    def start_stage(self, session_id: str, stage: str, command: str, log_path: str = "") -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO session_stages (session_id, stage, command, log_path, started_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, stage, command, log_path, _now()),
            )
            return int(cur.lastrowid)

    def finish_stage(self, stage_id: int, returncode: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE session_stages SET finished_at = ?, returncode = ? WHERE id = ?", (_now(), returncode, stage_id))

    def stages(self, session_id: str) -> list[Stage]:
        rows = self.conn.execute("SELECT * FROM session_stages WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
        return [Stage(**dict(r)) for r in rows]

    @staticmethod
    def _revision(row: sqlite3.Row) -> Revision:
        return Revision(
            sha=row["sha"],
            ref=row["ref"],
            profile=json.loads(row["profile_json"]),
            synced_at=row["synced_at"],
            last_used_at=row["last_used_at"],
        )
