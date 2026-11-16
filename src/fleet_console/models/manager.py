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
from itertools import pairwise
import json
from pathlib import Path
import shlex
import sys
from uuid import uuid4

import yaml

from fleet_console.fleetref.manager import ReadyRevision
from fleet_console.fleetref.profile import DUMMY_ENV
from fleet_console.models.compose import CONFIG_NAME, SessionSpec, job_priority_problem, make_run_id, write_config
from fleet_console.auth import Access
from fleet_console.compose.spec import model_route
from fleet_console.compose.preview import from_resolved
from fleet_console.compose.policy import check_gpu
from fleet_console.observe import mlsc
from fleet_console.runner import Runner, StageFiles, attach, interrupt_stage
from fleet_console.settings import Paths, Settings, scrub
from fleet_console.store import Session, Store
from fleet_console.store.db import LOCAL_OWNER

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
        self._submit_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._actions: dict[str, asyncio.Lock] = {}
        self.paused: Callable[[], set[str]] = set  # сессии, на которых идёт бенч: таймер стоит, гашение — с подтверждением

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

    def launch_options(self, session: Session) -> dict:
        """Параметры формы; для старых сессий overrides восстанавливаются из первой команды подъёма."""
        path = Path(session.dir) / "launch.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                options = {key: data[key] for key in ("recipe", "model_name", "replicas", "inference", "ttl_minutes", "extra")}
                options["extra"] = tuple(options["extra"])
                return options
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise SessionError(f"не прочитать параметры сессии: {path}") from exc
        for stage in self.stages(session.id):
            if stage.stage not in {"preflight", "create_job"}:
                continue
            try:
                argv = shlex.split(stage.command, comments=True)
                overrides = [b for a, b in pairwise(argv) if a == "-o"]
                # Служебная часть заканчивается output_dir; всё после неё — имя и пользовательские overrides.
                end = next(i for i, value in enumerate(overrides) if value.startswith("output_dir=")) + 1
                if not overrides[-1].startswith("main.run="):
                    raise ValueError("нет шага fleet")
                extra = overrides[end:-1]
                model_name = ""
                if extra and extra[0].startswith("mlsc.served_model_name="):
                    model_name = extra.pop(0).split("=", 1)[1]
                base = dict(value.split("=", 1) for value in overrides[:end])
                return {
                    "recipe": session.recipe, "model_name": model_name, "replicas": int(base["experiment.jobs"]),
                    "inference": session.inference, "ttl_minutes": session.ttl_minutes, "extra": tuple(extra),
                }
            except (ValueError, StopIteration, IndexError, KeyError):
                continue
        raise SessionError("параметры запуска не сохранились: нет launch.json и исходной команды подъёма")

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
        options = self.launch_options(session)
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
            model_name=session.model if self.store.request(session.id) else options["model_name"],
            extra=options["extra"],
            mlsc_base_url=self.settings.cluster.mlsc_base_url,
        )

    # --- действия --------------------------------------------------------
    async def start(
        self, revision: ReadyRevision, *, request_key: str | None = None, **kwargs,
    ) -> Session:
        async with self._submit_lock:
            access = kwargs.get("access")
            owner = access.id if access else LOCAL_OWNER
            key = request_key or uuid4().hex
            existing = self.store.request_by_key(owner, "session", key)
            if existing:
                return self.get(existing["id"])
            return await self._submit(revision, request_key=key, **kwargs)

    async def _submit(
        self,
        revision: ReadyRevision,
        *,
        request_key: str,
        recipe: str,
        model_name: str = "",
        replicas: int = 1,
        inference: str = "litellm",
        ttl_minutes: int | None = None,
        extra: tuple[str, ...] = (),
        source_id: str | None = None,
        access: Access | None = None,
    ) -> Session:
        groups = revision.profile.groups
        if recipe not in groups.get("mlsc/model", []):
            raise SessionError(f"рецепта mlsc/model={recipe} нет в ревизии {revision.info.sha[:8]}")
        if replicas < 1:
            raise SessionError("реплик должно быть не меньше одной")
        if not self.token():
            raise SessionError("нет MLSC_TOKEN: задайте в окружении сервиса или в secrets.env")
        agent, dataset = self.stubs(groups)
        config_text = None
        if source_id is not None:
            if access:
                access.require_admin()
            source = access.session(source_id) if access else self.get(source_id)
            if source.sha != revision.info.sha:
                raise SessionError("повтор сессии должен использовать исходный SHA fleet")
            try:
                config_text = (Path(source.dir) / CONFIG_NAME).read_text(encoding="utf-8")
            except OSError as exc:
                raise SessionError("исходный конфиг сессии не прочитать — повтор с тем же сетапом невозможен") from exc
        now = self.clock()
        pod = await self.mlsc_pod()
        base = run_id = make_run_id(model_name or recipe, now)
        suffix = 2
        while self.store.get_session(run_id) is not None:  # две сессии в одну секунду — редкость, но id должен быть уникален
            run_id = f"{base}-{suffix}"
            suffix += 1
        run_dir = self.paths.runs / run_id
        write_config(run_dir)
        spec = SessionSpec(
            run_id, revision.worktree, run_dir, recipe, replicas, inference, agent, dataset, pod,
            model_route(model_name or recipe, run_id), tuple(extra), self.settings.cluster.mlsc_base_url
        )
        ttl = ttl_minutes or self.settings.session_ttl_minutes
        if config_text is not None:
            spec.config.write_text(config_text, encoding="utf-8")
        (run_dir / "launch.json").write_text(
            json.dumps({"recipe": recipe, "model_name": model_name, "replicas": replicas, "inference": inference,
                        "ttl_minutes": ttl, "extra": list(extra)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        model = spec.model_name
        result = await self.runner.run(spec.argv("create_job", print_config=True), cwd=spec.worktree, env=DUMMY_ENV, timeout=300)
        if not result.ok:
            raise SessionError("Preflight: " + scrub(result.stderr or result.stdout, [self.token() or ""]))
        spec.resolved.write_text(scrub(result.stdout, [self.token() or ""]))
        try:
            preview = from_resolved(yaml.safe_load(spec.resolved.read_text()))
            if problem := job_priority_problem(preview.queue, preview.job_priority):
                raise ValueError(problem)
            _, user_limit = self.store.user_limits(access.id if access else LOCAL_OWNER, self.settings.quotas)
            gpu = check_gpu(preview.gpu_total, self.settings, user_limit=user_limit)
            if preview.model != model or preview.replicas != replicas:
                raise ValueError("Итоговый конфиг меняет маршрут модели или число реплик")
        except (ValueError, TypeError, yaml.YAMLError) as exc:
            raise SessionError(str(exc)) from exc
        session = Session(
            id=run_id, recipe=recipe, status="queued", created_at=_iso(now), ref=revision.ref, sha=revision.info.sha,
            worktree=str(revision.worktree), model=model, replicas=replicas, inference=inference, ttl_minutes=ttl,
            stage="preflight", dir=str(run_dir), owner_id=access.id if access else LOCAL_OWNER,
        )
        if not self.store.enqueue(session, gpu, request_key):
            return self.get(self.store.request_by_key(session.owner_id, "session", request_key)["id"])
        self.store.event("session_start", run_id, "ok", f"{recipe} × {replicas}, {inference}, ttl {ttl} мин")
        await self.dispatch()
        return self.get(run_id)

    async def dispatch(self) -> None:
        async with self._dispatch_lock:
            for session_id in self.store.queued("session"):
                try:
                    spec = await self.spec(self.get(session_id))
                except (SessionError, OSError, ValueError) as exc:
                    self.store.update_session(session_id, status="failed", error=str(exc))
                    self.store.release(session_id)
                    continue
                if self.store.admit(session_id, self.settings.quotas):
                    self._launch(session_id, spec, "preflight")

    async def stop(self, session_id: str, reason: str = "по кнопке", force: bool = False, *, access: Access | None = None) -> Session:
        async with self._actions.setdefault(session_id, asyncio.Lock()):
            return await self._stop(session_id, reason, force, access=access)

    async def _stop(self, session_id: str, reason: str = "по кнопке", force: bool = False, *, access: Access | None = None) -> Session:
        session = access.session(session_id) if access else self.get(session_id)
        if session.status == "queued":
            self.store.update_session(session_id, status="stopped", stage="", stopped_at=_iso(self.clock()))
            self.store.release(session_id)
            return self.get(session_id)
        if session_id in self.paused() and not force:
            raise SessionError("на сессии идёт бенч: остановите прогон или подтвердите гашение под идущим бенчем")
        self.store.update_session(session_id, status="stopping")
        task = self.tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()  # подъём прерван: дальше только гашение
            await asyncio.gather(task, return_exceptions=True)
        stage = self.stages(session_id)[-1:]
        if stage and stage[0].finished_at is None:
            files = StageFiles.for_stage(Path(session.dir) / "logs", stage[0].stage)
            try:
                await interrupt_stage(files)
            except RuntimeError as exc:
                self.store.update_session(session_id, error=str(exc))
                return self.get(session_id)
            self.store.finish_stage(stage[0].id, files.read_exit())
        session = self.get(session_id)
        if session.version_id is None and any(s.stage == "create_job" for s in self.stages(session_id)):
            try:
                version_id = mlsc.created_version(await self._configuration(), session_id)
                self.store.update_session(session_id, version_id=version_id)
                session = self.get(session_id)
                (Path(session.dir) / "version_id").write_text(str(version_id))
            except Exception as exc:
                self.store.update_session(session_id, error=str(exc))
                return self.get(session_id)
        if session.version_id is None:
            self.store.update_session(session_id, status="stopped", stage="", stopped_at=_iso(self.clock()), error="")
            self.store.event("session_stop", session_id, "ok", f"{reason}; версии MLSC не было, гасить нечего")
            self.store.release(session_id)
            return self.get(session_id)
        try:
            ok = await self._stage(session_id, await self.spec(session), "stop")
            ok = ok and mlsc.is_stopped(await self._configuration(), session.version_id)
        except Exception as exc:
            self.store.update_session(session_id, error=str(exc))
            return self.get(session_id)
        if ok:
            self.store.update_session(session_id, status="stopped", stage="", stopped_at=_iso(self.clock()), error="")
            self.store.release(session_id)
        else:
            self.store.update_session(session_id, error="MLSC не подтвердила гашение модели; резерв GPU сохранён")
        self.store.event("session_stop", session_id, "ok" if ok else "error", reason)
        return self.get(session_id)

    async def extend(self, session_id: str, minutes: int, *, access: Access | None = None) -> Session:
        session = access.session(session_id) if access else self.get(session_id)
        base = max(self.clock(), _parse(session.expires_at)) if session.expires_at else self.clock()
        self.store.update_session(session_id, expires_at=_iso(base + timedelta(minutes=minutes)), warned=0)
        self.store.event("session_extend", session_id, "ok", f"+{minutes} мин")
        return self.get(session_id)

    async def set_replicas(self, session_id: str, count: int, *, access: Access | None = None) -> Session:
        async with self._actions.setdefault(session_id, asyncio.Lock()):
            return await self._set_replicas(session_id, count, access=access)

    async def _set_replicas(self, session_id: str, count: int, *, access: Access | None = None) -> Session:
        if count < 0:
            raise SessionError("число реплик должно быть ≥ 0")
        if count == 0:
            return await self._stop(session_id, access=access)
        session = access.session(session_id) if access else self.get(session_id)
        if session.version_id is None:
            raise SessionError("версия MLSC ещё не создана")
        if self.store.request(session_id):
            preview = from_resolved(yaml.safe_load((Path(session.dir) / "resolved.yaml").read_text()))
            try:
                _, user_limit = self.store.user_limits(session.owner_id, self.settings.quotas)
                gpu = check_gpu(count * preview.gpu_per_instance, self.settings, user_limit=user_limit)
                self.store.grow_reservation(session_id, gpu, self.settings.quotas)
            except (ValueError, TypeError) as exc:
                raise SessionError(str(exc)) from exc
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
        """Продолжить подъём по журналу и сверить прерванные остановки с MLSC."""
        for session in self.sessions(active_only=True):
            if session.status == "starting":
                await self._resume_start(session.id)
            elif session.status == "stopping":
                await self._reconcile_stop(session.id)

    async def _resume_start(self, session_id: str) -> None:
        async with self._actions.setdefault(session_id, asyncio.Lock()):
            session = self.get(session_id)
            if session.status != "starting" or ((task := self.tasks.get(session_id)) and not task.done()):
                return
            try:
                spec = await self.spec(session)
                stages = self.stages(session_id)
                last = stages[-1] if stages else None
                if last and last.finished_at is None:
                    files = StageFiles.for_stage(Path(session.dir) / "logs", last.stage)
                    if last.stage in UP_STEPS[1:] and (files.alive() or files.read_exit() is not None):
                        self._launch(session_id, spec, last.stage, (last.id, files))
                        return
                    self.store.finish_stage(last.id, None)
                awaiting_version = any(stage.stage == "create_job" for stage in stages)
                if not awaiting_version and session.version_id is None:
                    self._launch(session_id, spec, "preflight")
                    return
                configuration = await self._configuration()
                version_id = session.version_id or spec.read_version_id()
                if awaiting_version:
                    version_id = mlsc.created_version(configuration, session_id, version_id)
                mlsc.replica_counts(configuration, version_id)
                spec.version_id_file.write_text(str(version_id))
                self.store.update_session(session_id, version_id=version_id)
                self._launch(session_id, spec, "start_llm")
            except Exception as exc:
                self.store.update_session(session_id, status="attention", stage="", error=str(exc))

    async def _reconcile_stop(self, session_id: str) -> None:
        lock = self._actions.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            session = self.get(session_id)
            if session.status != "stopping":
                return
            stages = self.stages(session_id)
            last = stages[-1] if stages else None
            if last and last.finished_at is None:
                files = StageFiles.for_stage(Path(session.dir) / "logs", last.stage)
                if files.alive():
                    return
                self.store.finish_stage(last.id, files.read_exit())
            try:
                version_id = session.version_id
                if version_id is not None or any(stage.stage == "create_job" for stage in stages):
                    configuration = await self._configuration()
                    if version_id is None:
                        version_id = mlsc.created_version(configuration, session_id)
                        (Path(session.dir) / "version_id").write_text(str(version_id))
                        self.store.update_session(session_id, version_id=version_id)
                    if not mlsc.is_stopped(configuration, version_id):
                        self.store.update_session(session_id, error=f"Остановка прервана: версия MLSC {version_id} ещё активна")
                        return
            except Exception as exc:
                self.store.update_session(session_id, error=f"Не удалось сверить остановку с MLSC: {exc}")
                return
            self.store.update_session(session_id, status="stopped", stage="", stopped_at=_iso(self.clock()), error="")
            self.store.release(session_id)
            self.store.event("session_stop", session_id, "ok", "после перезапуска MLSC подтвердила остановку")

    async def tick(self) -> None:
        """Таймер жизни: по истечении — уведомление, через льготный интервал — гашение."""
        for session in self.sessions(active_only=True):
            if session.status == "stopping":
                await self._reconcile_stop(session.id)
        await self.dispatch()
        now = self.clock()
        grace = timedelta(minutes=self.settings.session_grace_minutes)
        paused = self.paused()
        for session in self.sessions(active_only=True):
            if session.status != "ready" or not session.expires_at:
                continue
            if session.id in paused:  # на сессии идёт бенч: срок сдвигается, пока он не кончится
                self.store.update_session(session.id, expires_at=_iso(max(_parse(session.expires_at), now + grace)), warned=0)
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
    def _launch(self, session_id: str, spec: SessionSpec, from_step: str, attach_to: tuple[int, StageFiles] | None = None) -> None:
        if session_id in self.tasks and not self.tasks[session_id].done():
            return
        self.tasks[session_id] = asyncio.create_task(self._bring_up(session_id, spec, from_step, attach_to), name=f"session-{session_id}")

    async def _bring_up(self, session_id: str, spec: SessionSpec, from_step: str, attach_to: tuple[int, StageFiles] | None) -> None:
        try:
            for step in UP_STEPS[UP_STEPS.index(from_step):]:
                code = None
                if attach_to is not None:
                    stage_id, files = attach_to
                    code = await attach(files)
                    self.store.finish_stage(stage_id, code)
                    attach_to = None
                ok = code == 0 if code is not None else await (
                    self._preflight(session_id, spec) if step == "preflight" else self._stage(session_id, spec, step)
                )
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
        status = "attention" if session.version_id is not None or any(s.stage == "create_job" for s in self.stages(session_id)) else "failed"
        self.store.update_session(session_id, status=status, stage="", error=error)
        if status == "failed":
            self.store.release(session_id)
        self.store.event("session_fail", session_id, "error", error)

    async def _preflight(self, session_id: str, spec: SessionSpec) -> bool:
        """`--print-config` с теми же overrides: ловит ошибки конфига до того, как заняты GPU."""
        argv = spec.argv("create_job", print_config=True)
        stage_id = self.store.start_stage(session_id, "preflight", shlex.join(argv), str(spec.resolved))
        self.store.update_session(session_id, stage="preflight")
        # Только заглушки: --print-config резолвит oc.env, и настоящий токен попал бы в resolved.yaml (так и было до Ф3).
        result = await self.runner.run(argv, cwd=spec.worktree, env=DUMMY_ENV, timeout=300)
        self.store.finish_stage(stage_id, result.returncode)
        secrets = [self.token() or ""]
        if result.ok:
            spec.resolved.write_text(scrub(result.stdout, secrets), encoding="utf-8")
            if request := self.store.request(session_id):
                preview = from_resolved(yaml.safe_load(result.stdout))
                if problem := job_priority_problem(preview.queue, preview.job_priority):
                    raise SessionError(problem)
                if preview.model != spec.model_name or preview.gpu_total != request["gpu"]:
                    raise SessionError("Конфиг модели изменился после принятия заявки; создайте новую сессию")
        else:
            (spec.run_dir / "logs" / "preflight.log").write_text(scrub(result.stderr or result.stdout, secrets), encoding="utf-8")
        return result.ok

    async def _stage(self, session_id: str, spec: SessionSpec, step: str, *, jobs: int | None = None) -> bool:
        if step == "create_job" and any(s.stage == "create_job" for s in self.stages(session_id)):
            version_id = mlsc.created_version(await self._configuration(), session_id,
                                              self.get(session_id).version_id or spec.read_version_id())
            spec.version_id_file.write_text(str(version_id))
            return True
        token = self.token()
        if not token:
            raise SessionError("нет MLSC_TOKEN: задайте в окружении сервиса или в secrets.env")
        argv = spec.argv(step, jobs=jobs)
        files = StageFiles.for_stage(spec.run_dir / "logs", step)
        files.clear()
        stage_id = self.store.start_stage(session_id, step, shlex.join(argv) + "  # env: MLSC_TOKEN", str(files.log))
        self.store.update_session(session_id, stage=step)
        proc = await self.runner.detach(argv, cwd=spec.worktree, env={"MLSC_TOKEN": token}, files=files)
        code = await proc.wait()
        self.store.finish_stage(stage_id, code)
        return code == 0

    async def _configuration(self) -> dict:
        c = self.settings.cluster
        return await mlsc.get_configuration(self.runner, c.mlsc_namespace, await self.mlsc_pod(), c.mlsc_base_url, self.token() or "")

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
