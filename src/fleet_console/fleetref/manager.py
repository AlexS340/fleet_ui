"""RevisionManager: ревизия от ссылки до готового worktree с профилем."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

from fleet_console.fleetref import profile as prof
from fleet_console.fleetref.git import Branch, Commit, CommitInfo, Mirror
from fleet_console.fleetref.sync import uv_sync, venv_ready
from fleet_console.runner import CommandError, Runner
from fleet_console.settings import Paths, Settings
from fleet_console.store import Store

SyncGate = Callable[[], str | None]  # None = SOCKS доступен, иначе uv sync пробует только прямое подключение


class PrepareError(RuntimeError):
    pass


@dataclass
class Progress:
    """Что сейчас делает менеджер; UI читает по таймеру."""

    phase: str = "idle"  # idle | clone | fetch | worktree | sync | profile | done | error
    ref: str = ""
    sha: str = ""
    error: str = ""
    started_at: float | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=40))

    @property
    def busy(self) -> bool:
        return self.phase in {"clone", "fetch", "worktree", "sync", "profile"}

    def start(self, phase: str, ref: str = "", sha: str = "") -> None:
        self.phase, self.ref, self.sha, self.error = phase, ref or self.ref, sha or self.sha, ""
        self.started_at = time.monotonic()
        self.log.clear()

    def fail(self, message: str) -> None:
        self.phase, self.error = "error", message


@dataclass(frozen=True)
class ReadyRevision:
    ref: str
    info: CommitInfo
    worktree: Path
    profile: prof.Profile
    from_cache: bool


class RevisionManager:
    def __init__(
        self,
        mirror: Mirror,
        store: Store,
        runner: Runner,
        paths: Paths,
        settings: Settings,
        *,
        sync_gate: SyncGate = lambda: None,
    ) -> None:
        self.mirror = mirror
        self.store = store
        self.runner = runner
        self.paths = paths
        self.settings = settings
        self.sync_gate = sync_gate
        self.progress = Progress()
        self.current: ReadyRevision | None = None
        self._ready: dict[str, ReadyRevision] = {}
        self._lock = asyncio.Lock()

    def selected(self, owner_id: str) -> ReadyRevision | None:
        if self.current:
            self._ready[self.current.info.sha] = self.current
        sha = self.store.selected_revision(owner_id)
        if sha is None and self.current:
            sha = self.current.info.sha
            self.store.select_revision(owner_id, sha)
        return self._ready.get(sha) if sha else None

    async def select(self, owner_id: str, ref: str) -> ReadyRevision:
        revision = await self.prepare(ref)
        self.store.select_revision(owner_id, revision.info.sha)
        return revision

    # --- зеркало ---------------------------------------------------------
    async def ensure_mirror(self) -> None:
        if self.mirror.exists():
            return
        async with self._lock:
            self.progress.start("clone")
            try:
                await self.mirror.ensure(on_line=self.progress.log.append)
            except CommandError as exc:
                self.progress.fail(f"clone не удался: {exc}")
                self.store.event("clone", self.mirror.remote, "error", str(exc))
                raise PrepareError(self.progress.error) from exc
            self.progress.phase = "done"
            self.store.event("clone", self.mirror.remote, "ok")

    async def fetch(self) -> None:
        async with self._lock:
            self.progress.start("fetch")
            try:
                await self.mirror.fetch()
            except CommandError as exc:
                self.progress.fail(f"fetch не удался: {exc}")
                self.store.event("fetch", self.mirror.remote, "error", str(exc))
                raise PrepareError(self.progress.error) from exc
            self.progress.phase = "done"
            self.store.event("fetch", self.mirror.remote, "ok")

    async def branches(self) -> list[Branch]:
        return await self.mirror.branches()

    async def commits(self, ref: str, limit: int = 50) -> list[Commit]:
        return await self.mirror.commits(ref, limit)

    async def commit_info(self, ref: str) -> CommitInfo:
        return await self.mirror.commit_info(await self.mirror.resolve(ref))

    # --- ревизия ---------------------------------------------------------
    def is_ready(self, sha: str) -> bool:
        return venv_ready(self.mirror.worktree_path(sha)) and self.store.get_revision(sha) is not None

    async def prepare(self, ref: str) -> ReadyRevision:
        """Resolve → worktree → uv sync (если нужно) → профиль (кеш по SHA)."""
        async with self._lock:
            self.progress.start("worktree", ref=ref)
            try:
                sha = await self.mirror.resolve(ref)
                self.progress.sha = sha
                info = await self.mirror.commit_info(sha)
                worktree = await self.mirror.worktree_add(sha)
                if not await self.mirror.is_clean(worktree):
                    raise PrepareError(f"worktree {worktree} не чистый — сервис в него не пишет, разберитесь руками")
                synced_now = False
                if not venv_ready(worktree):
                    proxy = self.settings.bastion.socks_proxy or None
                    if proxy and self.sync_gate():
                        proxy = None
                    self.progress.phase = "sync"
                    result = await uv_sync(
                        self.runner,
                        worktree,
                        log_path=self.paths.logs / f"sync-{sha[:12]}.log",
                        proxy=proxy,
                        on_line=self.progress.log.append,
                    )
                    if not result.ok:
                        tail = " | ".join(list(self.progress.log)[-3:])
                        raise PrepareError(f"uv sync завершился с кодом {result.returncode}: {tail}")
                    synced_now = True
                cached = None if synced_now else self.store.get_revision(sha)
                if cached is not None:
                    profile = prof.Profile.from_dict(cached.profile)
                    self.store.touch_revision(sha)
                else:
                    self.progress.phase = "profile"
                    profile = await prof.build(self.runner, worktree, sha)
                    self.store.upsert_revision(sha, profile.to_dict(), ref)
                self.store.event("prepare", f"{ref}@{sha[:8]}", "ok", "кеш" if cached else "собран профиль")
            except (CommandError, PrepareError) as exc:
                self.progress.fail(str(exc))
                self.store.event("prepare", ref, "error", str(exc))
                raise PrepareError(str(exc)) from exc
            self.progress.phase = "done"
            self.current = ReadyRevision(ref, info, worktree, profile, from_cache=cached is not None)
            self._ready[sha] = self.current
            return self.current

    async def file_at(self, sha: str, rel: str) -> str | None:
        """Файл группы конфигов (путь относительно каталога конфигов) на ревизии из зеркала; нет — None."""
        try:
            return await self.mirror.show(sha, (prof.CONFIGS_REL / rel).as_posix())
        except CommandError:
            return None

    async def restore(self) -> ReadyRevision | None:
        """После перезапуска сервиса: вернуть последнюю использованную ревизию, если её worktree и профиль на месте.

        Иначе форма прогона и сессии отказывали, пока не нажмёшь «Подготовить ревизию» (грабли Ф2).
        """
        if self.current is not None or self.progress.busy:
            return self.current
        for rev in self.store.revisions():
            if not self.is_ready(rev.sha):
                continue
            try:
                default = await self.prepare(rev.sha)
                for row in self.store.conn.execute("SELECT DISTINCT sha FROM revision_choices").fetchall():
                    if self.is_ready(row["sha"]) and row["sha"] not in self._ready:
                        await self.prepare(row["sha"])
                self.current = default
                return default
            except PrepareError:
                return None
        return None

    async def gc_worktrees(self, ttl_days: int | None = None) -> list[str]:
        """Удалить worktree, не использовавшиеся дольше TTL (активных прогонов в Ф0 нет)."""
        ttl = timedelta(days=self.settings.worktree_ttl_days if ttl_days is None else ttl_days)
        now = datetime.now(timezone.utc)
        removed: list[str] = []
        for sha in self.mirror.worktrees():
            if (self.store.conn.execute("SELECT 1 FROM drafts WHERE sha=?", (sha,)).fetchone()
                    or self.store.conn.execute("SELECT 1 FROM revision_choices WHERE sha=?", (sha,)).fetchone()
                    or any(r.sha == sha for r in [*self.store.benches(True), *self.store.sessions(True)])):
                continue
            rev = self.store.get_revision(sha)
            if rev is not None:
                last_used = datetime.fromisoformat(rev.last_used_at)
            else:
                last_used = datetime.fromtimestamp(self.mirror.worktree_path(sha).stat().st_mtime, tz=timezone.utc)
            if now - last_used < ttl:
                continue
            await self.mirror.worktree_remove(sha)
            self.store.delete_revision(sha)
            self.store.event("worktree-gc", sha[:8], "ok", f"не использовался {(now - last_used).days} дн.")
            removed.append(sha)
        return removed
