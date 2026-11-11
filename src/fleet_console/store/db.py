"""SQLite: события (аудит), кеш профилей ревизий по SHA, свои прогоны, сессии моделей, бенч-прогоны."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

LOCAL_OWNER = "@local"

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
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin'))
);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    sha TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS revision_choices (
    owner_id TEXT PRIMARY KEY,
    sha TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('bench', 'session')),
    owner_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    gpu INTEGER NOT NULL CHECK (gpu >= 0),
    slots INTEGER NOT NULL DEFAULT 1,
    session_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('queued', 'reserved', 'released')),
    created_at TEXT NOT NULL,
    UNIQUE(owner_id, kind, request_key)
);
CREATE TABLE IF NOT EXISTS measurements (
    run_id TEXT PRIMARY KEY REFERENCES bench_runs(id),
    sha TEXT NOT NULL,
    measured_at TEXT NOT NULL,
    results_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS measurement_links (
    digest TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES measurements(run_id),
    expires_at TEXT NOT NULL,
    revoked_at TEXT
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
CREATE TABLE IF NOT EXISTS bench_runs (
    id TEXT PRIMARY KEY,
    preset TEXT NOT NULL DEFAULT '',
    ref TEXT NOT NULL DEFAULT '',
    sha TEXT NOT NULL DEFAULT '',
    worktree TEXT NOT NULL DEFAULT '',
    dir TEXT NOT NULL DEFAULT '',
    recipe TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    agent TEXT NOT NULL DEFAULT '',
    dataset TEXT NOT NULL DEFAULT '',
    jobs INTEGER NOT NULL DEFAULT 1,
    agents_per_replica INTEGER NOT NULL DEFAULT 1,
    task_limit INTEGER,
    inference TEXT NOT NULL DEFAULT 'litellm',
    session_id TEXT,
    status TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    version_id INTEGER,
    error TEXT NOT NULL DEFAULT '',
    manual INTEGER NOT NULL DEFAULT 0,
    requeues INTEGER NOT NULL DEFAULT 0,
    agent_limit INTEGER,
    counters_json TEXT NOT NULL DEFAULT '{}',
    preview_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    started_at TEXT,
    attention_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS bench_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    command TEXT NOT NULL,
    log_path TEXT NOT NULL DEFAULT '',
    pid INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    returncode INTEGER
);
"""

ACTIVE_SESSION_STATUSES = ("queued", "starting", "ready", "stopping", "attention")
ACTIVE_BENCH_STATUSES = ("queued", "running", "attention", "stopping")


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
    owner_id: str = LOCAL_OWNER

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
    owner_id: str = LOCAL_OWNER

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


@dataclass
class BenchRun:
    """Бенч-прогон, запущенный сервисом: стадии fleet по журналу, своя или сессионная модель."""

    id: str  # он же experiment.run_id и лейбл fleet/run-name
    status: str  # running | attention | stopping | stopped | failed | done
    created_at: str
    preset: str = ""
    ref: str = ""
    sha: str = ""
    worktree: str = ""
    dir: str = ""
    recipe: str = ""
    model: str = ""
    agent: str = ""
    dataset: str = ""  # имя группы harbor/dataset или JSON {path, benchmark, mapping}
    jobs: int = 1
    agents_per_replica: int = 1
    task_limit: int | None = None
    inference: str = "litellm"
    session_id: str | None = None  # бенч на уже поднятой сессии модели: подъём и гашение пропускаются
    stage: str = ""
    version_id: int | None = None
    error: str = ""
    manual: int = 0  # ручные правки конфига в каталоге прогона
    requeues: int = 0  # сделано раундов requeue
    agent_limit: int | None = None  # последнее записанное значение файла лимита
    counters_json: str = "{}"
    preview_json: str = "{}"
    started_at: str | None = None
    attention_at: str | None = None
    finished_at: str | None = None
    owner_id: str = LOCAL_OWNER

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_BENCH_STATUSES

    @property
    def own_model(self) -> bool:
        return self.session_id is None

    @property
    def holds_gpu(self) -> bool:
        """Своя версия MLSC создана и не погашена."""
        return self.own_model and self.version_id is not None and self.status not in {"stopped", "failed", "done"}

    @property
    def counters(self) -> dict[str, Any]:
        return json.loads(self.counters_json or "{}")

    @property
    def preview(self) -> dict[str, Any]:
        return json.loads(self.preview_json or "{}")


@dataclass(frozen=True)
class BenchStage:
    id: int
    run_id: str
    stage: str
    command: str
    log_path: str
    pid: int | None
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
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        with self.conn:
            if not any(row["name"] == "sha" for row in self.conn.execute("PRAGMA table_info(drafts)")):
                self.conn.execute("ALTER TABLE drafts ADD COLUMN sha TEXT NOT NULL DEFAULT ''")
            for table in ("runs", "sessions", "bench_runs"):
                if not any(row["name"] == "owner_id" for row in self.conn.execute(f"PRAGMA table_info({table})")):
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN owner_id TEXT NOT NULL DEFAULT '{LOCAL_OWNER}'")

    def register_user(self, user_id: str, admin: bool = False) -> None:
        if not admin and self.conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
            return
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
            if admin:
                self.conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (user_id,))

    def is_admin(self, user_id: str) -> bool:
        row = self.conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        return row is not None and row["role"] == "admin"

    def migrate_owner(self, owner_id: str) -> None:
        with self.conn:
            for table in ("runs", "sessions", "bench_runs", "drafts"):
                self.conn.execute(f"UPDATE {table} SET owner_id = ? WHERE owner_id = ?", (owner_id, LOCAL_OWNER))

    def reserve_draft(self, draft_id: str, owner_id: str, sha: str = "") -> None:
        with self.conn:
            self.conn.execute("INSERT INTO drafts (id, owner_id, sha) VALUES (?, ?, ?)", (draft_id, owner_id, sha))

    def select_revision(self, owner_id: str, sha: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO revision_choices VALUES (?, ?) ON CONFLICT(owner_id) DO UPDATE SET sha=excluded.sha", (owner_id, sha))

    def selected_revision(self, owner_id: str) -> str | None:
        row = self.conn.execute("SELECT sha FROM revision_choices WHERE owner_id=?", (owner_id,)).fetchone()
        return row["sha"] if row else None

    def request(self, request_id: str):
        return self.conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()

    def request_by_key(self, owner_id: str, kind: str, key: str):
        return self.conn.execute("SELECT * FROM requests WHERE owner_id=? AND kind=? AND request_key=?", (owner_id, kind, key)).fetchone()

    def enqueue(self, obj: BenchRun | Session, gpu: int, request_key: str) -> bool:
        """Заявка и объект фиксируются вместе, до любого внешнего действия."""
        kind = "bench" if isinstance(obj, BenchRun) else "session"
        table = "bench_runs" if kind == "bench" else "sessions"
        session_id = obj.session_id if kind == "bench" else None
        columns = [f.name for f in fields(obj)]
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO requests VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                (obj.id, kind, obj.owner_id, request_key, gpu, 0 if session_id else 1, session_id, obj.created_at),
            )
            if not cur.rowcount:
                return False
            self.conn.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                              [getattr(obj, c) for c in columns])
            if kind == "bench":
                self._adopt_run(obj.id, obj.version_id, obj.model, "запущен сервисом", obj.owner_id)
                self.conn.execute("DELETE FROM drafts WHERE id=? AND owner_id=?", (obj.id, obj.owner_id))
        return True

    def admit(self, request_id: str, quotas) -> bool:
        """Проверка обеих квот и резерв в одной SQLite write-транзакции, включая разные соединения."""
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            request = self.request(request_id)
            if request is None or request["state"] != "queued":
                return False
            active = list(self.conn.execute("SELECT owner_id, gpu, slots FROM requests WHERE state='reserved'"))
            # Старые активные объекты без резерва: неизвестный расход блокирует новые GPU,
            # пока оператор не завершит их. Терминальные записи историю квот не занимают.
            for table, statuses in (("bench_runs", ACTIVE_BENCH_STATUSES), ("sessions", ACTIVE_SESSION_STATUSES)):
                active += list(self.conn.execute(
                    f"SELECT owner_id, ? AS gpu, 1 AS slots FROM {table} WHERE status IN ({','.join('?' * len(statuses))})"
                    " AND id NOT IN (SELECT id FROM requests)", (quotas.total_gpus, *statuses),
                ))
            if request["session_id"]:
                session = self.get_session(request["session_id"])
                if session is None or session.status != "ready" or session.owner_id != request["owner_id"]:
                    return False
                if self.conn.execute("SELECT 1 FROM requests WHERE session_id=? AND state='reserved'", (session.id,)).fetchone():
                    return False
            own = [r for r in active if r["owner_id"] == request["owner_id"]]
            if (sum(r["slots"] for r in active) + request["slots"] > quotas.total_runs
                    or sum(r["slots"] for r in own) + request["slots"] > quotas.user_runs
                    or sum(r["gpu"] for r in active) + request["gpu"] > quotas.total_gpus
                    or sum(r["gpu"] for r in own) + request["gpu"] > quotas.user_gpus):
                return False
            self.conn.execute("UPDATE requests SET state='reserved' WHERE id=?", (request_id,))
            table, status = ("bench_runs", "running") if request["kind"] == "bench" else ("sessions", "starting")
            self.conn.execute(f"UPDATE {table} SET status=? WHERE id=?", (status, request_id))
            return True

    def queued(self, kind: str) -> list[str]:
        return [r["id"] for r in self.conn.execute("SELECT id FROM requests WHERE kind=? AND state='queued' ORDER BY created_at, rowid", (kind,))]

    def release(self, request_id: str) -> None:
        with self.conn:
            request = self.request(request_id)
            if request and request["kind"] == "session" and self.conn.execute(
                "SELECT 1 FROM requests WHERE session_id=? AND state='reserved'", (request_id,),
            ).fetchone():
                return
            self.conn.execute("UPDATE requests SET state='released' WHERE id=?", (request_id,))
            if request and request["session_id"]:
                session = self.get_session(request["session_id"])
                if session and session.status == "stopped":
                    self.conn.execute("UPDATE requests SET state='released' WHERE id=?", (session.id,))

    def grow_reservation(self, request_id: str, gpu: int, quotas) -> None:
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            request = self.request(request_id)
            if request is None:
                return  # управление прежними усыновлёнными ресурсами
            if request["state"] != "reserved":
                raise ValueError("Ресурсы этой заявки не зарезервированы")
            if gpu <= request["gpu"]:
                return  # уменьшение освобождается только после подтверждённого гашения
            rows = list(self.conn.execute("SELECT owner_id, gpu FROM requests WHERE state='reserved' AND id<>?", (request_id,)))
            if (gpu + sum(r["gpu"] for r in rows) > quotas.total_gpus
                    or gpu + sum(r["gpu"] for r in rows if r["owner_id"] == request["owner_id"]) > quotas.user_gpus):
                raise ValueError("Для увеличения числа реплик не хватает квоты GPU")
            self.conn.execute("UPDATE requests SET gpu=? WHERE id=?", (gpu, request_id))

    def requeue_request(self, request_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE requests SET state='queued' WHERE id=? AND state='released'", (request_id,))

    def draft_owner(self, draft_id: str) -> str | None:
        row = self.conn.execute("SELECT owner_id FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return row["owner_id"] if row else None

    def delete_draft(self, draft_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))

    def save_measurement(self, run_id: str, sha: str, measured_at: str, results: list[dict]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO measurements VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET"
                " sha=excluded.sha, measured_at=excluded.measured_at, results_json=excluded.results_json",
                (run_id, sha, measured_at, json.dumps(results, ensure_ascii=False)),
            )

    def measurements(self, owner_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT m.* FROM measurements m JOIN bench_runs b ON b.id=m.run_id"
            " WHERE b.owner_id=? ORDER BY m.measured_at DESC", (owner_id,),
        ).fetchall()

    def measurement(self, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM measurements WHERE run_id=?", (run_id,)).fetchone()

    def add_measurement_link(self, digest: str, run_id: str, expires_at: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO measurement_links (digest, run_id, expires_at) VALUES (?, ?, ?)", (digest, run_id, expires_at))

    def measurement_links(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT digest, expires_at, revoked_at FROM measurement_links WHERE run_id=? ORDER BY expires_at DESC", (run_id,),
        ).fetchall()

    def revoke_measurement_link(self, run_id: str, digest: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE measurement_links SET revoked_at=? WHERE run_id=? AND digest=? AND revoked_at IS NULL",
                (_now(), run_id, digest),
            )

    def shared_measurement(self, digest: str, now: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT m.* FROM measurements m JOIN measurement_links l ON l.run_id=m.run_id"
            " WHERE l.digest=? AND l.expires_at>? AND l.revoked_at IS NULL", (digest, now),
        ).fetchone()

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
    def adopt_run(self, run_id: str, version_id: int | None = None, model: str = "", note: str = "", *, owner_id: str = LOCAL_OWNER) -> None:
        with self.conn:
            self._adopt_run(run_id, version_id, model, note, owner_id)

    def _adopt_run(self, run_id: str, version_id: int | None, model: str, note: str, owner_id: str) -> None:
        result = self.conn.execute(
            "INSERT INTO runs (run_id, version_id, model, note, adopted_at, owner_id) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(run_id) DO UPDATE SET version_id=excluded.version_id, model=excluded.model, note=excluded.note"
            " WHERE runs.owner_id=excluded.owner_id", (run_id, version_id, model, note, _now(), owner_id),
        )
        if result.rowcount == 0:
            raise ValueError("Прогон уже зарегистрирован за другим владельцем")

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

    # --- бенч-прогоны ----------------------------------------------------
    def create_bench(self, run: BenchRun, *, own_note: str | None = None) -> None:
        cols = [f.name for f in fields(BenchRun)]
        with self.conn:
            self.conn.execute(
                f"INSERT INTO bench_runs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [getattr(run, c) for c in cols],
            )
            if own_note is not None:
                self._adopt_run(run.id, run.version_id, run.model, own_note, run.owner_id)
                self.conn.execute("DELETE FROM drafts WHERE id = ? AND owner_id = ?", (run.id, run.owner_id))

    def update_bench(self, run_id: str, **values: Any) -> None:
        if not values:
            return
        assignments = ", ".join(f"{k} = ?" for k in values)
        with self.conn:
            self.conn.execute(f"UPDATE bench_runs SET {assignments} WHERE id = ?", [*values.values(), run_id])

    def get_bench(self, run_id: str) -> BenchRun | None:
        row = self.conn.execute("SELECT * FROM bench_runs WHERE id = ?", (run_id,)).fetchone()
        return BenchRun(**dict(row)) if row else None

    def benches(self, active_only: bool = False) -> list[BenchRun]:
        sql = "SELECT * FROM bench_runs"
        if active_only:
            sql += " WHERE status IN (" + ", ".join("?" * len(ACTIVE_BENCH_STATUSES)) + ")"
        rows = self.conn.execute(sql + " ORDER BY created_at DESC", ACTIVE_BENCH_STATUSES if active_only else ()).fetchall()
        return [BenchRun(**dict(r)) for r in rows]

    def start_bench_stage(self, run_id: str, stage: str, command: str, log_path: str = "", pid: int | None = None) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO bench_stages (run_id, stage, command, log_path, pid, started_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, stage, command, log_path, pid, _now()),
            )
            return int(cur.lastrowid)

    def set_bench_stage_pid(self, stage_id: int, pid: int | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE bench_stages SET pid = ? WHERE id = ?", (pid, stage_id))

    def finish_bench_stage(self, stage_id: int, returncode: int | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE bench_stages SET finished_at = ?, returncode = ? WHERE id = ?", (_now(), returncode, stage_id))

    def bench_stages(self, run_id: str) -> list[BenchStage]:
        rows = self.conn.execute("SELECT * FROM bench_stages WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        return [BenchStage(**dict(r)) for r in rows]

    def open_bench_stage(self, run_id: str) -> BenchStage | None:
        """Последняя стадия без кода выхода — та, что шла, когда сервис перезапустили."""
        row = self.conn.execute(
            "SELECT * FROM bench_stages WHERE run_id = ? AND finished_at IS NULL ORDER BY id DESC LIMIT 1", (run_id,)
        ).fetchone()
        return BenchStage(**dict(row)) if row else None

    @staticmethod
    def _revision(row: sqlite3.Row) -> Revision:
        return Revision(
            sha=row["sha"],
            ref=row["ref"],
            profile=json.loads(row["profile_json"]),
            synced_at=row["synced_at"],
            last_used_at=row["last_used_at"],
        )
