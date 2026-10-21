"""Бенч-прогон по стадиям fleet: короткие подпроцессы в своей группе процессов, долгое ожидание задач — у сервиса.

Стадии: preflight (--print-config) → materialize (до GPU) → create_job → start_llm → check → upload → launch →
execute (цикл сервиса: лимит агентов = живые реплики × агенты на реплику, опрос Argo и сабмиттера) →
[requeue → launch → execute] → stop_llm → measure. На сессии модели подъём и гашение пропускаются.

Сбой до GPU — «не удался»; сбой после — «требует внимания» (баннер, уведомление macOS, автогашение без ответа).
После перезапуска сервиса: живой PID стадии — прицепиться; мёртвый — повторить по правилу стадии.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shlex
import sys
from typing import Any

import yaml

from fleet_console.compose import BenchSpec, Preview, SessionLink
from fleet_console.fleetref.profile import DUMMY_ENV
from fleet_console.models import SessionManager
from fleet_console.observe import ClusterObserver
from fleet_console.observe import argo, mlsc, s3
from fleet_console.observe.argo import TaskWorkflow
from fleet_console.observe.s3 import S3Object, aws_env
from fleet_console.presets import DatasetRef
from fleet_console.runner import CommandError, Runner, StageFiles, attach, kill_stage
from fleet_console.settings import Paths, Settings, scrub
from fleet_console.store import BenchRun, Store

Clock = Callable[[], datetime]
PodFinder = Callable[[], Awaitable[str]]
Sleeper = Callable[[float], Awaitable[None]]

STAGE_STEPS: dict[str, list[str]] = {
    "preflight": [],
    "materialize": ["system.materialize", "system.topology_saver", "system.manifest_runner"],
    "create_job": ["mlsc.create_job"],
    "start_llm": ["mlsc.start_llm"],
    "check": ["mlsc.check_llm_works"],
    "upload": ["mlsc.create_agent_limit", "system.runtime_upload", "system.task_artifacts_upload", "system.pod_upload"],
    "launch": ["system.submitter_launch"],
    "execute": [],
    "requeue": ["system.requeue_failed"],
    "stop_llm": ["mlsc.stop_llm"],
    "measure": ["system.measure_run"],
}
STAGE_TITLES = {
    "preflight": "подготовка: --print-config",
    "materialize": "материализация задач (до GPU)",
    "create_job": "подъём модели: create_job",
    "start_llm": "подъём модели: start_llm",
    "check": "подъём модели: check_llm_works",
    "upload": "сабмит: лимит агентов и загрузка в S3 и на под",
    "launch": "сабмит: запуск сабмиттера",
    "execute": "исполнение: ждём задачи, держим лимит агентов",
    "requeue": "requeue упавших",
    "stop_llm": "гашение модели",
    "measure": "замер (Benchboard)",
}
BEFORE_GPU = ("preflight", "materialize")
REQUEUED_RE = re.compile(r"Перенесено submitted → pending: (\d+)/(\d+)")
MEASURE_RE = re.compile(r"^\s*(\S+/\S+): (\d+(?:\.\d+)?)% \((.*)\)\s*$")  # строка benchboard на лист benchmark/agent


class BenchError(RuntimeError):
    pass


def plan(own_model: bool, requeue_rounds: int) -> list[str]:
    stages = ["preflight", "materialize"]
    if own_model:
        stages += ["create_job", "start_llm", "check"]
    stages += ["upload", "launch", "execute"]
    for _ in range(max(0, requeue_rounds)):
        stages += ["requeue", "launch", "execute"]
    if own_model:
        stages.append("stop_llm")
    stages.append("measure")
    return stages


def position(stages: list[str], stage: str, requeues: int) -> int:
    """Индекс стадии в плане с учётом пройденных раундов requeue (launch и execute повторяются)."""
    done = 0
    for i, name in enumerate(stages):
        if name == stage and done == requeues:
            return i
        if name == "requeue":
            done += 1
    raise BenchError(f"стадии {stage} нет в плане прогона")


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class BenchManager:
    def __init__(
        self,
        store: Store,
        runner: Runner,
        paths: Paths,
        settings: Settings,
        *,
        secrets: Callable[[], dict[str, str]],
        mlsc_pod: PodFinder,
        observer: ClusterObserver,
        sessions: SessionManager,
        agent_limit: Callable[[str, int], Awaitable[Any]],
        stop_tasks: Callable[[str], Awaitable[Any]],
        clock: Clock = lambda: datetime.now(timezone.utc),
        sleep: Sleeper = asyncio.sleep,
        platform: str = sys.platform,
    ) -> None:
        self.store = store
        self.runner = runner
        self.paths = paths
        self.settings = settings
        self.secrets = secrets
        self.mlsc_pod = mlsc_pod
        self.observer = observer
        self.sessions = sessions
        self.agent_limit = agent_limit
        self.stop_tasks = stop_tasks
        self.clock = clock
        self.sleep = sleep
        self.platform = platform
        self.tasks: dict[str, asyncio.Task] = {}

    # --- чтение ------------------------------------------------------------
    def runs(self, active_only: bool = False) -> list[BenchRun]:
        return self.store.benches(active_only)

    def get(self, run_id: str) -> BenchRun:
        run = self.store.get_bench(run_id)
        if run is None:
            raise BenchError(f"прогона {run_id} нет в журнале")
        return run

    def stages(self, run_id: str):
        return self.store.bench_stages(run_id)

    def plan_of(self, run: BenchRun) -> list[str]:
        return plan(run.own_model, self.settings.bench.requeue_rounds)

    def busy_sessions(self) -> set[str]:
        """Сессии, на которых идёт бенч: их таймер стоит, гасить без подтверждения нельзя."""
        return {r.session_id for r in self.runs(active_only=True) if r.session_id}

    def session_link(self, session_id: str) -> SessionLink:
        s = self.sessions.get(session_id)
        return SessionLink(s.id, s.model, s.recipe, s.replicas, s.version_id)

    async def spec_of(self, run: BenchRun) -> BenchSpec:
        """Спецификация из spec.json каталога прогона; под MLSC и данные сессии — свежие."""
        path = Path(run.dir) / "spec.json"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BenchError(f"нет {path}: каталог прогона удалён?") from exc
        session = self.session_link(run.session_id) if run.session_id else None
        return BenchSpec.from_json(text, pod=await self._pod(), session=session)

    # --- карточка: задачи, логи, S3 -----------------------------------------------
    def aws_env(self) -> dict[str, str]:
        """Окружение `aws` CLI: ключи fleet и свой config с endpoint — команда повторяется в терминале как есть."""
        return aws_env(self.secrets(), self.paths.home / "aws.cfg", self.settings.s3_endpoint, self.settings.s3_region)

    async def task_workflows(self, run_id: str) -> list[TaskWorkflow]:
        return await argo.list_tasks(self.runner, self.settings.namespace, run_id)

    async def task_log(self, workflow: str, container: str = "main") -> str:
        return await argo.pod_logs(self.runner, self.settings.namespace, workflow, container)

    def s3_base(self, run_id: str) -> str:
        base = str(self.get(run_id).preview.get("s3") or "")
        if not base:
            raise BenchError("в превью прогона нет пути S3: конфиг не собирался")
        return base

    async def s3_objects(self, run_id: str) -> list[S3Object]:
        return await s3.ls(self.runner, self.s3_base(run_id), self.aws_env())

    async def s3_text(self, uri: str) -> str:
        return await s3.cat(self.runner, uri, self.aws_env())

    async def model_log(self, run_id: str) -> str:
        """Последний упавший воркер версии MLSC — единственный лог модели, который отдаёт API."""
        run = self.get(run_id)
        version_id = run.version_id
        if version_id is None and run.session_id:
            version_id = self.sessions.get(run.session_id).version_id
        if version_id is None:
            raise BenchError("version_id модели неизвестен: модель ещё не создавалась")
        token = self.secrets().get("MLSC_TOKEN", "")
        if not token:
            raise BenchError("нет MLSC_TOKEN")
        c = self.settings.cluster
        return await mlsc.get_last_failed_log(self.runner, c.mlsc_namespace, await self.mlsc_pod(), c.mlsc_base_url, token, version_id)

    def measure_summary(self, run_id: str) -> list[str]:
        """Строки итогов benchboard из лога стадии measure: `<benchmark>/<agent>: <score>% (…)`."""
        try:
            text = (Path(self.get(run_id).dir) / "logs" / "measure.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return [line.strip() for line in text.splitlines() if MEASURE_RE.match(line)]

    async def _pod(self) -> str:
        try:
            return await self.mlsc_pod()
        except Exception:  # noqa: BLE001 — без кластера берём имя пода из mlsc/common.yaml ревизии
            return ""

    # --- подготовка черновика ------------------------------------------------
    def env_for_fleet(self) -> dict[str, str]:
        env = dict(self.secrets())
        root = self.settings.datasets_root_path()
        if root is not None:
            env["FLEET_DATASETS_ROOT"] = str(root)
        return env

    async def print_config(self, spec: BenchSpec, run_id: str | None = None) -> tuple[dict[str, Any] | None, str]:
        """`--print-config` с заглушками секретов: resolved.yaml без токенов; ошибка Hydra — текстом."""
        argv = spec.argv(["system.materialize"], print_config=True)
        env = {**DUMMY_ENV, **({"FLEET_DATASETS_ROOT": str(self.settings.datasets_root_path())} if self.settings.datasets_root_path() else {})}
        stage_id = self.store.start_bench_stage(run_id, "preflight", shlex.join(argv), str(spec.resolved)) if run_id else None
        result = await self.runner.run(argv, cwd=spec.worktree, env=env, timeout=300)
        if stage_id is not None:
            self.store.finish_bench_stage(stage_id, result.returncode)
        if not result.ok:
            error = scrub((result.stderr or result.stdout).strip(), self.secrets().values())
            (spec.logs_dir / "preflight.log").write_text(error + "\n", encoding="utf-8")
            return None, "\n".join(error.splitlines()[-6:])
        text = scrub(result.stdout, self.secrets().values())
        spec.resolved.write_text(text, encoding="utf-8")
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            return None, f"--print-config вернул не YAML: {exc}"
        return (data if isinstance(data, dict) else None), ""

    # --- действия ------------------------------------------------------------
    async def start(self, spec: BenchSpec, *, ref: str, preview: Preview) -> BenchRun:
        if self.store.get_bench(spec.run_id) is not None:
            raise BenchError(f"прогон {spec.run_id} уже есть")
        if spec.session and self.sessions.get(spec.session.id).status != "ready":
            raise BenchError("сессия модели не готова")
        spec.write()
        now = self.clock()
        run = BenchRun(
            id=spec.run_id, status="running", created_at=_iso(now), started_at=_iso(now), preset=spec.preset, ref=ref, sha=spec.sha,
            worktree=str(spec.worktree), dir=str(spec.run_dir), recipe=spec.recipe, model=spec.effective_model or preview.model,
            agent=spec.agent, dataset=spec.dataset if isinstance(spec.dataset, str) else spec.dataset.model_dump_json(),
            jobs=spec.effective_jobs, agents_per_replica=spec.agents_per_replica, task_limit=spec.task_limit, inference=spec.inference,
            session_id=spec.session.id if spec.session else None, stage="preflight", preview_json=preview.to_json(),
            version_id=spec.session.version_id if spec.session else None,
        )
        self.store.create_bench(run)
        self.store.adopt_run(run.id, run.version_id, model=run.model, note=f"запущен сервисом · пресет {spec.preset}" if spec.preset else "запущен сервисом")
        self.store.event("bench_start", run.id, "ok", f"{run.model} × {spec.dataset_label} × {spec.agent}; {run.jobs} × {run.agents_per_replica}")
        self._launch(run.id, spec, 0)
        return run

    async def retry(self, run_id: str) -> BenchRun:
        """«Повторить стадию»: с той, на которой упали."""
        run = self.get(run_id)
        if run.status not in {"attention", "failed", "stopped"}:
            raise BenchError("повторять можно только упавшую или остановленную стадию")
        if not run.stage:
            raise BenchError("стадия неизвестна")
        spec = await self.spec_of(run)
        self.store.update_bench(run_id, status="running", error="", attention_at=None)
        self.store.event("bench_retry", run_id, "ok", run.stage)
        self._launch(run_id, spec, position(self.plan_of(run), run.stage, run.requeues))
        return self.get(run_id)

    async def stop(self, run_id: str, reason: str = "по кнопке") -> BenchRun:
        """Остановка: убить стадию, лимит 0 → сабмиттер → Terminate workflows → mlsc.stop_llm, если модель своя."""
        run = self.get(run_id)
        task = self.tasks.pop(run_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        spec = await self.spec_of(run)
        open_stage = self.store.open_bench_stage(run_id)
        if open_stage is not None:
            files = StageFiles.for_stage(spec.logs_dir, open_stage.stage)
            kill_stage(files)
            self.store.finish_bench_stage(open_stage.id, None)
        self.store.update_bench(run_id, status="stopping", error="")
        failed: list[str] = []
        try:
            report = await self.stop_tasks(run_id)
            failed.extend(getattr(report, "failed", []))
        except Exception as exc:  # noqa: BLE001 — остановку задач показываем, но модель всё равно гасим
            failed.append(f"задачи: {exc}")
        if run.own_model and run.version_id is not None:
            ok = await self._stage(run_id, spec, "stop_llm")
            if not ok:
                failed.append("mlsc.stop_llm завершился с ошибкой — модель может быть не погашена")
        status = "attention" if failed else "stopped"
        self.store.update_bench(run_id, status=status, stage="" if not failed else "stop_llm", finished_at=_iso(self.clock()), error="; ".join(failed))
        self.store.event("bench_stop", run_id, "ok" if not failed else "error", reason + ("; " + "; ".join(failed) if failed else ""))
        return self.get(run_id)

    async def measure(self, run_id: str) -> BenchRun:
        """Замер по кнопке для остановленного или упавшего прогона: что долетело до S3, то и посчитается."""
        run = self.get(run_id)
        if run.active:
            raise BenchError("прогон ещё идёт: замер будет последней стадией")
        spec = await self.spec_of(run)
        previous = run.status
        self.store.update_bench(run_id, status="running", stage="measure")
        ok = await self._stage(run_id, spec, "measure")
        self.store.update_bench(run_id, status="done" if ok else previous, stage="" if ok else "measure", error="" if ok else "замер завершился с ошибкой — см. лог")
        self.store.event("bench_measure", run_id, "ok" if ok else "error")
        return self.get(run_id)

    async def resume(self) -> None:
        """После перезапуска: сверить журнал с диском и кластером, прицепиться к живой стадии или повторить мёртвую."""
        for run in self.runs(active_only=True):
            if run.status == "stopping":
                self.store.update_bench(run.id, status="attention", attention_at=_iso(self.clock()), error="сервис перезапущен во время остановки — проверьте workflows и MLSC")
                continue
            if run.status == "attention":
                continue
            try:
                spec = await self.spec_of(run)
            except BenchError as exc:
                self.store.update_bench(run.id, status="failed", stage="", error=str(exc))
                continue
            stages = self.plan_of(run)
            open_stage = self.store.open_bench_stage(run.id)
            if open_stage is None:
                index = position(stages, run.stage or "preflight", run.requeues)
                self._launch(run.id, spec, index)
                continue
            files = StageFiles.for_stage(spec.logs_dir, open_stage.stage)
            if open_stage.stage not in ("execute", "preflight") and (files.alive() or files.read_exit() is not None):
                self._launch(run.id, spec, position(stages, open_stage.stage, run.requeues), attach_to=(open_stage.id, files))
                continue
            self.store.finish_bench_stage(open_stage.id, None)
            if open_stage.stage == "create_job":
                version_id = spec.read_version_id()
                if version_id is None:
                    self._attention(run.id, "сервис перезапущен во время mlsc.create_job, version_id не записан — проверьте MLSC руками")
                    continue
                self.store.update_bench(run.id, version_id=version_id)
                self.store.adopt_run(run.id, version_id, model=run.model, note="запущен сервисом")
                self._launch(run.id, spec, position(stages, "start_llm", run.requeues))
                continue
            self.store.event("bench_resume", run.id, "ok", f"стадия {open_stage.stage} повторяется")
            self._launch(run.id, spec, position(stages, open_stage.stage, run.requeues))

    async def tick(self) -> None:
        """Без ответа в «требует внимания» N минут — гасим модель сами (если включено)."""
        minutes = self.settings.bench.attention_minutes
        if minutes <= 0:
            return
        now = self.clock()
        for run in self.runs(active_only=True):
            if run.status != "attention" or not run.holds_gpu or not run.attention_at:
                continue
            if now >= datetime.fromisoformat(run.attention_at) + timedelta(minutes=minutes):
                await self.stop(run.id, reason=f"без ответа {minutes} мин — автогашение")

    # --- внутреннее ------------------------------------------------------------
    def _launch(self, run_id: str, spec: BenchSpec, index: int, attach_to: tuple[int, StageFiles] | None = None) -> None:
        self.tasks[run_id] = asyncio.create_task(self._drive(run_id, spec, index, attach_to), name=f"bench-{run_id}")

    async def _drive(self, run_id: str, spec: BenchSpec, index: int, attach_to: tuple[int, StageFiles] | None = None) -> None:
        run = self.get(run_id)
        stages = self.plan_of(run)
        try:
            if attach_to is not None:
                stage_id, files = attach_to
                stage = stages[index]
                self.store.update_bench(run_id, stage=stage, status="running")
                code = await attach(files)
                self.store.finish_bench_stage(stage_id, code)
                self.store.event("bench_resume", run_id, "ok", f"прицепился к стадии {stage}: код {code}")
                if not await self._after(run_id, spec, stage, code == 0):
                    return
                index += 1
            while index < len(stages):
                stage = stages[index]
                self.store.update_bench(run_id, stage=stage, status="running")
                if stage == "execute":
                    ok = await self._execute(run_id, spec)
                elif stage == "preflight":
                    resolved, error = await self.print_config(spec, run_id)
                    ok = resolved is not None
                    if not ok:
                        self.store.update_bench(run_id, error=error)
                elif stage == "launch" and await self._submitter_alive(run_id):
                    sid = self.store.start_bench_stage(run_id, "launch", "пропущено: сабмиттер прогона уже жив на поде (повторный submitter_launch дал бы второй процесс)")
                    self.store.finish_bench_stage(sid, 0)
                    ok = True
                else:
                    ok = await self._stage(run_id, spec, stage)
                if not await self._after(run_id, spec, stage, ok):
                    return
                if stage == "requeue" and not self._requeued(spec):
                    index += 2  # нечего перепосылать: launch и execute этого раунда не нужны
                    self.store.event("bench_stage", run_id, "ok", "requeue: переносить нечего, раунд пропущен")
                index += 1
            self.store.update_bench(run_id, status="done", stage="", error="", finished_at=_iso(self.clock()))
            self.store.event("bench_done", run_id, "ok")
            await self._notify("Fleet Console", f"Прогон {run_id} завершён: замер готов")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — любая ошибка видна в карточке, а не в stderr сервиса
            self._fail(run_id, self.get(run_id).stage or "прогон", spec, str(exc))

    async def _after(self, run_id: str, spec: BenchSpec, stage: str, ok: bool) -> bool:
        """Переход после стадии: version_id после create_job, счётчик requeue; False — прогон остановлен сбоем."""
        if ok and stage == "create_job":
            version_id = spec.read_version_id()
            if version_id is None:
                self._fail(run_id, stage, spec, "mlsc.create_job завершился, но файл version_id пуст")
                return False
            run = self.get(run_id)
            self.store.update_bench(run_id, version_id=version_id)
            self.store.adopt_run(run_id, version_id, model=run.model, note="запущен сервисом")
        if ok and stage == "requeue":
            self.store.update_bench(run_id, requeues=self.get(run_id).requeues + 1)
        if not ok:
            self._fail(run_id, stage, spec)
        return ok

    def _requeued(self, spec: BenchSpec) -> bool:
        try:
            text = (spec.logs_dir / "requeue.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        matches = REQUEUED_RE.findall(text)
        return bool(matches) and int(matches[-1][0]) > 0

    def _fail(self, run_id: str, stage: str, spec: BenchSpec, detail: str = "") -> None:
        tail = detail or _log_tail(spec.logs_dir / f"{stage}.log") or self.get(run_id).error  # у execute лога нет: причина в журнале
        error = f"стадия {stage} не удалась" + (f": {tail}" if tail else "")
        if stage in BEFORE_GPU:
            self.store.update_bench(run_id, status="failed", error=error, finished_at=_iso(self.clock()))
            self.store.event("bench_fail", run_id, "error", error)
        else:
            self._attention(run_id, error)

    def _attention(self, run_id: str, error: str) -> None:
        run = self.get(run_id)
        self.store.update_bench(run_id, status="attention", error=error, attention_at=_iso(self.clock()))
        self.store.event("bench_attention", run_id, "error", error)
        what = "модель поднята, GPU тратятся" if run.holds_gpu or (run.own_model and run.version_id is not None) else "задачи могут идти"
        asyncio.ensure_future(self._notify("Fleet Console", f"Прогон {run_id} требует внимания: {what}. {error[:120]}"))

    async def _stage(self, run_id: str, spec: BenchSpec, stage: str) -> bool:
        env = self.env_for_fleet()
        argv = spec.argv(STAGE_STEPS[stage])
        files = StageFiles.for_stage(spec.logs_dir, stage)
        names = ", ".join(sorted(env)) or "—"
        stage_id = self.store.start_bench_stage(run_id, stage, shlex.join(argv) + f"  # env: {names}", str(files.log))
        proc = await self.runner.detach(argv, cwd=spec.worktree, env=env, files=files)
        self.store.set_bench_stage_pid(stage_id, proc.pid)
        try:
            code = await proc.wait()
        except asyncio.CancelledError:
            raise
        self.store.finish_bench_stage(stage_id, code)
        return code == 0

    async def _snapshot(self, run_id: str, spec: BenchSpec) -> dict[str, Any]:
        """Свежие данные дашборда: прогон Argo, каталог на сабмиттере, живые реплики модели."""
        obs = self.observer
        stale = obs.runs.at is None or (datetime.now(timezone.utc) - obs.runs.at).total_seconds() > self.settings.bench.poll_seconds
        if stale and not obs.busy:
            await obs.refresh()
        argo = next((r for r in obs.runs.data or [] if r.run_name == run_id), None)
        sub = next((s for s in obs.submitter.data or [] if s.run_id == run_id), None)
        run = self.get(run_id)
        version_id = spec.session.version_id if spec.session else run.version_id
        version = next((v for v in obs.mlsc.data or [] if v.version_id == version_id), None) if version_id is not None else None
        return {"argo": argo, "sub": sub, "version": version}

    async def _submitter_alive(self, run_id: str) -> bool:
        try:
            snap = await self._snapshot(run_id, await self.spec_of(self.get(run_id)))
        except Exception:  # noqa: BLE001 — не видно сабмиттера → запускаем как обычно
            return False
        return bool(snap["sub"] and snap["sub"].alive)

    async def _execute(self, run_id: str, spec: BenchSpec) -> bool:
        cfg = self.settings.bench
        stage_id = self.store.start_bench_stage(
            run_id, "execute", "цикл сервиса: лимит агентов = живые реплики × агенты на реплику; опрос Argo и сабмиттера; завершение, когда очередь пуста и активных workflows нет"
        )
        started = self.clock()
        seen_alive = False
        last_limit = self.get(run_id).agent_limit
        try:
            while True:
                snap = await self._snapshot(run_id, spec)
                argo, sub, version = snap["argo"], snap["sub"], snap["version"]
                live = version.current if version is not None else None
                desired = live * spec.agents_per_replica if live is not None else None
                if desired is not None and desired != last_limit:
                    try:
                        await self.agent_limit(run_id, desired)
                        last_limit = desired
                        self.store.update_bench(run_id, agent_limit=desired)
                    except (CommandError, RuntimeError) as exc:
                        self.store.event("bench_limit", run_id, "error", str(exc))
                counters = {
                    "running": argo.running if argo else 0,
                    "pending_wf": argo.pending if argo else 0,
                    "succeeded": argo.succeeded if argo else 0,
                    "failed": argo.failed if argo else 0,
                    "queue": sub.pending if sub else None,
                    "submitted": sub.submitted if sub else None,
                    "completed": sub.completed if sub else None,
                    "submitter_alive": bool(sub and sub.alive),
                    "replicas": live,
                    "at": _iso(self.clock()),
                }
                self.store.update_bench(run_id, counters_json=json.dumps(counters))
                alive = bool(sub and sub.alive)
                seen_alive = seen_alive or alive
                queue = sub.pending if sub else 0
                active = argo.active if argo else 0
                elapsed = (self.clock() - started).total_seconds()
                started_ok = seen_alive or (sub is not None and (sub.submitted + sub.completed) > 0) or elapsed >= cfg.startup_grace_seconds
                if started_ok and not alive and active == 0:
                    if sub is None:
                        self.store.finish_bench_stage(stage_id, 1)
                        self.store.update_bench(run_id, error="каталога прогона на submitter-поде нет: сабмит не состоялся")
                        return False
                    if queue > 0:
                        self.store.finish_bench_stage(stage_id, 1)
                        self.store.update_bench(run_id, error=f"сабмиттер не жив, а в очереди {queue} манифестов — повторите стадию launch")
                        return False
                    self.store.finish_bench_stage(stage_id, 0)
                    return True
                await self.sleep(cfg.poll_seconds)
        except asyncio.CancelledError:
            self.store.finish_bench_stage(stage_id, None)
            raise

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


def dataset_of(run: BenchRun) -> str | DatasetRef:
    return DatasetRef.model_validate_json(run.dataset) if run.dataset.startswith("{") else run.dataset
