"""Сессии моделей: подъём шагами fleet, таймер жизни, гашение, возобновление после перезапуска.

Стадии подъёма: preflight (`--print-config` → resolved.yaml, GPU ещё не заняты) → mlsc.create_job
(не идемпотентен: version_id сразу в журнал, повтор начинается со start_llm) → mlsc.start_llm →
mlsc.check_llm_works. Каждая стадия — отдельный подпроцесс fleet с логом в каталоге сессии и точной
командой в журнале. Сбой после create_job переводит сессию в «attention»: GPU могут быть заняты.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shlex
import sys

from fleet_console.fleetref.manager import ReadyRevision
from fleet_console.fleetref.profile import DUMMY_ENV
from fleet_console.models.compose import SessionSpec, make_run_id, write_config
from fleet_console.runner import Runner
from fleet_console.settings import Paths, Settings
from fleet_console.store import Session, Store

Clock = Callable[[], datetime]
PodFinder = Callable[[], Awaitable[str]]
UP_STEPS = ("preflight", "create_job", "start_llm", "check")


class SessionError(RuntimeError):
    pass


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


class SessionManager:
    def __init__(
        self,
        store: Store,
        runner: Runner,
        paths: Paths,
        settings: Settings,
        *,
        token: Callable[[], str | None],
        mlsc_pod: PodFinder,
        clock: Clock = lambda: datetime.now(timezone.utc),
        platform: str = sys.platform,
    ) -> None:
        self.store = store
        self.runner = runner
        self.paths = paths
        self.settings = settings
        self.token = token
        self.mlsc_pod = mlsc_pod
        self.clock = clock
        self.platform = platform
        self.tasks: dict[str, asyncio.Task] = {}

    # --- чтение ----------------------------------------------------------
    def sessions(self, active_only: bool = False) -> list[Session]:
        return self.store.sessions(active_only)

    def get(self, session_id: str) -> Session:
        session = self.store.get_session(session_id)
        if session is None:
            raise SessionError(f"сессии {session_id} нет в журнале")
        return session

    def stages(self, session_id: str):
        return self.store.stages(session_id)

    def stubs(self, groups: dict[str, list[str]]) -> tuple[str, str]:
        try:
            return groups["harbor/agent"][0], groups["harbor/dataset"][0]
        except (KeyError, IndexError):
            raise SessionError("в ревизии нет групп harbor/agent и harbor/dataset — заглушек для конфига сессии нет") from None

    async def spec(self, session: Session) -> SessionSpec:
        """Собрать спецификацию из журнала: заглушки — из кеша профиля ревизии."""
        revision = self.store.get_revision(session.sha)
        if revision is None:
            raise SessionError(f"профиль ревизии {session.sha[:8]} не в кеше — подготовьте её заново")
        agent, dataset = self.stubs(revision.profile.get("groups", {}))
        return SessionSpec(
            run_id=session.id,
            worktree=Path(session.worktree),
            run_dir=Path(session.dir),
            recipe=session.recipe,
            replicas=session.replicas,
            inference=session.inference,
            stub_agent=agent,
            stub_dataset=dataset,
            pod=await self.mlsc_pod(),
            model_name=session.model,
        )

    # --- действия --------------------------------------------------------
    async def start(
        self,
        revision: ReadyRevision,
        *,
        recipe: str,
        model_name: str = "",
        replicas: int = 1,
        inference: str = "litellm",
        ttl_minutes: int | None = None,
        extra: tuple[str, ...] = (),
    ) -> Session:
        groups = revision.profile.groups
        if recipe not in groups.get("mlsc/model", []):
            raise SessionError(f"рецепта mlsc/model={recipe} нет в ревизии {revision.info.sha[:8]}")
        if replicas < 1:
            raise SessionError("реплик должно быть не меньше одной")
        if not self.token():
            raise SessionError("нет MLSC_TOKEN: задайте в окружении сервиса или в secrets.env")
        agent, dataset = self.stubs(groups)
        now = self.clock()
        base = run_id = make_run_id(model_name or recipe, now)
        suffix = 2
        while self.store.get_session(run_id) is not None:  # две сессии в одну секунду — редкость, но id должен быть уникален
            run_id = f"{base}-{suffix}"
            suffix += 1
        run_dir = self.paths.runs / run_id
        write_config(run_dir)
        spec = SessionSpec(
            run_id, revision.worktree, run_dir, recipe, replicas, inference, agent, dataset, await self.mlsc_pod(), model_name, tuple(extra)
        )
        ttl = ttl_minutes or self.settings.session_ttl_minutes
        session = Session(
            id=run_id, recipe=recipe, status="starting", created_at=_iso(now), ref=revision.ref, sha=revision.info.sha,
            worktree=str(revision.worktree), model=model_name, replicas=replicas, inference=inference, ttl_minutes=ttl,
            stage="preflight", dir=str(run_dir),
        )
        self.store.create_session(session)
        self.store.event("session_start", run_id, "ok", f"{recipe} × {replicas}, {inference}, ttl {ttl} мин")
        self._launch(run_id, spec, "preflight")
        return session

    async def stop(self, session_id: str, reason: str = "по кнопке") -> Session:
        session = self.get(session_id)
        task = self.tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()  # подъём прерван: дальше только гашение
        if session.version_id is None:
            self.store.update_session(session_id, status="stopped", stage="", stopped_at=_iso(self.clock()), error="")
            self.store.event("session_stop", session_id, "ok", f"{reason}; версии MLSC не было, гасить нечего")
            return self.get(session_id)
        self.store.update_session(session_id, status="stopping")
        ok = await self._stage(session_id, await self.spec(session), "stop")
        if ok:
            self.store.update_session(session_id, status="stopped", stage="", stopped_at=_iso(self.clock()), error="")
        else:
            self.store.update_session(session_id, status="attention", error="mlsc.stop_llm завершился с ошибкой — модель может быть не погашена")
        self.store.event("session_stop", session_id, "ok" if ok else "error", reason)
        return self.get(session_id)

    async def extend(self, session_id: str, minutes: int) -> Session:
        session = self.get(session_id)
        base = max(self.clock(), _parse(session.expires_at)) if session.expires_at else self.clock()
        self.store.update_session(session_id, expires_at=_iso(base + timedelta(minutes=minutes)), warned=0)
        self.store.event("session_extend", session_id, "ok", f"+{minutes} мин")
        return self.get(session_id)

    async def set_replicas(self, session_id: str, count: int) -> Session:
        if count < 0:
            raise SessionError("число реплик должно быть ≥ 0")
        session = self.get(session_id)
        if session.version_id is None:
            raise SessionError("версия MLSC ещё не создана")
        ok = await self._stage(session_id, await self.spec(session), "start_llm", jobs=count)
        if ok:
            self.store.update_session(session_id, replicas=count, stage="")
        else:
            self.store.update_session(session_id, stage="", error="mlsc.start_llm завершился с ошибкой при смене реплик")
        self.store.event("session_replicas", session_id, "ok" if ok else "error", f"planned={count}")
        if not ok:
            raise SessionError("mlsc.start_llm завершился с ошибкой — см. лог стадии")
        return self.get(session_id)

    async def resume(self) -> None:
        """После перезапуска сервиса: подъём продолжить со start_llm, если version_id известен; иначе — честно сдаться."""
        for session in self.sessions(active_only=True):
            if session.status == "starting":
                if session.version_id is None:
                    self.store.update_session(
                        session.id, status="failed", stage="", error="сервис перезапущен до записи version_id — проверьте MLSC руками"
                    )
                    continue
                try:
                    self._launch(session.id, await self.spec(session), "start_llm")
                except SessionError as exc:
                    self.store.update_session(session.id, status="attention", stage="", error=str(exc))
            elif session.status == "stopping":
                self.store.update_session(session.id, status="attention", error="сервис перезапущен во время гашения — проверьте MLSC")

    async def tick(self) -> None:
        """Таймер жизни: по истечении — уведомление, через льготный интервал — гашение."""
        now = self.clock()
        grace = timedelta(minutes=self.settings.session_grace_minutes)
        for session in self.sessions(active_only=True):
            if session.status != "ready" or not session.expires_at:
                continue
            expires = _parse(session.expires_at)
            if now < expires:
                continue
            if not session.warned:
                self.store.update_session(session.id, warned=1)
                await self._notify(
                    "Fleet Console",
                    f"Сессия {session.model or session.recipe} истекла: погашу через {self.settings.session_grace_minutes} мин, если не продлить",
                )
                self.store.event("session_expired", session.id, "ok", f"гашение через {self.settings.session_grace_minutes} мин")
            elif now >= expires + grace:
                await self.stop(session.id, reason="истекло время жизни")

    # --- внутреннее ------------------------------------------------------
    def _launch(self, session_id: str, spec: SessionSpec, from_step: str) -> None:
        self.tasks[session_id] = asyncio.create_task(self._bring_up(session_id, spec, from_step), name=f"session-{session_id}")

    async def _bring_up(self, session_id: str, spec: SessionSpec, from_step: str) -> None:
        try:
            for step in UP_STEPS[UP_STEPS.index(from_step):]:
                ok = await (self._preflight(session_id, spec) if step == "preflight" else self._stage(session_id, spec, step))
                if ok and step == "create_job":
                    version_id = spec.read_version_id()
                    if version_id is None:
                        ok = False
                    else:
                        self.store.update_session(session_id, version_id=version_id)
                if not ok:
                    self._fail(session_id, step, spec)
                    return
            now = self.clock()
            session = self.get(session_id)
            self.store.update_session(
                session_id, status="ready", stage="", error="", ready_at=_iso(now), expires_at=_iso(now + timedelta(minutes=session.ttl_minutes))
            )
            self.store.event("session_ready", session_id, "ok", f"version_id={session.version_id}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — любая ошибка подъёма видна в карточке, а не в stderr сервиса
            self.store.update_session(session_id, stage="", error=str(exc))
            self._fail(session_id, self.get(session_id).stage or "подъём", spec, str(exc))

    def _fail(self, session_id: str, step: str, spec: SessionSpec, detail: str = "") -> None:
        session = self.get(session_id)
        tail = detail or _log_tail(spec.run_dir / "logs" / f"{step}.log")
        error = f"стадия {step} не удалась" + (f": {tail}" if tail else "")
        status = "attention" if session.version_id is not None else "failed"  # после create_job GPU могут быть заняты
        self.store.update_session(session_id, status=status, stage="", error=error)
        self.store.event("session_fail", session_id, "error", error)

    async def _preflight(self, session_id: str, spec: SessionSpec) -> bool:
        """`--print-config` с теми же overrides: ловит ошибки конфига до того, как заняты GPU."""
        argv = spec.argv("create_job", print_config=True)
        stage_id = self.store.start_stage(session_id, "preflight", shlex.join(argv), str(spec.resolved))
        self.store.update_session(session_id, stage="preflight")
        env = {**DUMMY_ENV, "MLSC_TOKEN": self.token() or ""}
        result = await self.runner.run(argv, cwd=spec.worktree, env=env, timeout=300)
        self.store.finish_stage(stage_id, result.returncode)
        if result.ok:
            spec.resolved.write_text(result.stdout, encoding="utf-8")
        else:
            (spec.run_dir / "logs" / "preflight.log").write_text(result.stderr or result.stdout, encoding="utf-8")
        return result.ok

    async def _stage(self, session_id: str, spec: SessionSpec, step: str, *, jobs: int | None = None) -> bool:
        token = self.token()
        if not token:
            raise SessionError("нет MLSC_TOKEN: задайте в окружении сервиса или в secrets.env")
        argv = spec.argv(step, jobs=jobs)
        log_path = spec.run_dir / "logs" / f"{step}.log"
        stage_id = self.store.start_stage(session_id, step, shlex.join(argv) + "  # env: MLSC_TOKEN", str(log_path))
        self.store.update_session(session_id, stage=step)
        result = await self.runner.stream(argv, cwd=spec.worktree, env={"MLSC_TOKEN": token}, log_path=log_path)
        self.store.finish_stage(stage_id, result.returncode)
        return result.ok

    async def _notify(self, title: str, text: str) -> None:
        if self.platform != "darwin":
            return
        script = f"display notification {json.dumps(text, ensure_ascii=False)} with title {json.dumps(title)}"
        await self.runner.run(["osascript", "-e", script], timeout=10)


def _log_tail(path: Path, lines: int = 3) -> str:
    try:
        tail = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-lines:]
    except OSError:
        return ""
    return " | ".join(tail)
