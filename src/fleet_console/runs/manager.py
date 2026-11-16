"""Бенч-прогон по стадиям fleet: короткие подпроцессы в своей группе процессов, долгое ожидание задач — у сервиса.

Стадии: preflight (--print-config) → materialize (до GPU) → create_job → start_llm → check → upload → launch →
execute (цикл сервиса: лимит агентов = живые реплики × агенты на реплику, опрос Argo и сабмиттера) →
[requeue → launch → execute] → stop_llm → measure. На сессии модели подъём и гашение пропускаются.

Сбой до GPU — «не удался»; сбой после — «требует внимания» (баннер, уведомление macOS, автогашение без ответа).
После перезапуска сервиса: живой PID стадии — прицепиться; мёртвый — повторить по правилу стадии.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shlex
import sys
from typing import Any

import yaml

from fleet_console.auth import Access, AccessDenied
from fleet_console.compose import BenchSpec, Preview, SessionLink, from_resolved
from fleet_console.compose.policy import check_gpu, private_root, supports_private_measure
from fleet_console.control.runs import active_workflows
from fleet_console.fleetref.profile import DUMMY_ENV
from fleet_console.models import SessionManager
from fleet_console.models.compose import job_priority_problem
from fleet_console.observe import ClusterObserver
from fleet_console.observe import argo, mlsc, s3, submitter
from fleet_console.observe.argo import TaskWorkflow
from fleet_console.observe.s3 import S3Object, aws_env
from fleet_console.presets import DatasetRef
from fleet_console.runner import CommandError, Runner, StageFiles, attach, interrupt_stage
from fleet_console.settings import Paths, Settings, scrub
from fleet_console.store import BenchRun, Store
from fleet_console.store.db import LOCAL_OWNER

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
MEASURE_COUNTS = re.compile(r"^[\d.eE+-]+/(\d+), failed=(\d+), timeout=(\d+), no_attempt=(\d+)")


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
        self._submit_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._actions: dict[str, asyncio.Lock] = {}

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
        return replace(BenchSpec.from_json(text, pod=await self._pod(), session=session), mlsc_base_url=self.settings.cluster.mlsc_base_url)

    # --- карточка: задачи, логи, S3 -----------------------------------------------
    def aws_env(self) -> dict[str, str]:
        """Окружение `aws` CLI: ключи fleet и свой config с endpoint — команда повторяется в терминале как есть."""
        return aws_env(self.secrets(), self.paths.home / "aws.cfg", self.settings.s3_endpoint, self.settings.s3_region, profile=self.settings.s3_read_profile)

    async def task_workflows(self, run_id: str, *, access: Access | None = None) -> list[TaskWorkflow]:
        if access:
            access.run(run_id)
        return await argo.list_tasks(self.runner, self.settings.namespace, run_id)

    async def task_log(self, workflow: str, container: str = "main", *, run_id: str | None = None, access: Access | None = None) -> str:
        if access and (run_id is None or not any(task.name == workflow for task in await self.task_workflows(run_id, access=access))):
            raise AccessDenied()
        return await argo.pod_logs(self.runner, self.settings.namespace, workflow, container)

    def s3_base(self, run_id: str) -> str:
        base = str(self.get(run_id).preview.get("s3") or "")
        if not base:
            raise BenchError("в превью прогона нет пути S3: конфиг не собирался")
        return base

    async def s3_objects(self, run_id: str, *, access: Access | None = None) -> list[S3Object]:
        if access:
            access.run(run_id)
        return await s3.ls(self.runner, self.s3_base(run_id), self.aws_env())

    async def s3_text(self, uri: str, *, run_id: str | None = None, access: Access | None = None) -> str:
        if access:
            if run_id is None:
                raise AccessDenied()
            access.run(run_id)
            base = self.s3_base(run_id).rstrip("/") + "/"
            if not uri.startswith(base) or any(part in {"", ".", ".."} for part in uri[len(base):].split("/")):
                raise AccessDenied()
        return await s3.cat(self.runner, uri, self.aws_env())

    async def model_log(self, run_id: str, *, access: Access | None = None) -> str:
        """Последний упавший воркер версии MLSC — единственный лог модели, который отдаёт API."""
        run = access.run(run_id) if access else self.get(run_id)
        version_id = run.version_id
        if run.session_id:
            session = access.session(run.session_id) if access else self.sessions.get(run.session_id)
            version_id = session.version_id
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
        last_measure = text.rsplit("Measure-run: ", 1)[-1]
        return [line.strip() for line in last_measure.splitlines() if MEASURE_RE.match(line)]

    async def save_measurement(self, run_id: str, *, measured_at: str | None = None, read_s3: bool = True) -> None:
        run = self.get(run_id)
        path = Path(run.dir) / "measurement.json"
        if self.store.request(run_id) and path.is_file():
            payload = json.loads(path.read_text())
            if payload.get("run_id") != run_id or payload.get("sha") != run.sha or not isinstance(payload.get("results"), list):
                raise BenchError("Структурированный замер относится к другой заявке или ревизии")
            self.store.save_measurement(run_id, run.sha, measured_at or _iso(self.clock()), payload["results"])
            return
        if self.store.request(run_id):
            raise BenchError("Шаг замера не записал measurement.json; результат не подтверждён")
        results = []
        for line in self.measure_summary(run_id):
            match = MEASURE_RE.fullmatch(line)
            leaf, score, counts = match.groups()
            totals = MEASURE_COUNTS.match(counts)
            if totals is None or any(part in {"", ".", ".."} for part in leaf.split("/")):
                continue
            planned, errors, timeouts, unattempted = map(int, totals.groups())
            result = {"leaf": leaf, "score": float(score), "planned": planned, "infra_errors": errors,
                      "timeouts": timeouts, "unattempted": unattempted, "succeeded": None}
            if read_s3 and not self.store.request(run_id) and run.preview.get("s3"):
                try:
                    uri = f"{self.s3_base(run_id).rstrip('/')}/{leaf}/_viewer/summary.json"
                    summary = json.loads(await self.s3_text(uri))
                    count = summary["resolved_tasks"]
                    if summary.get("run_id") == run_id and type(count) is int and 0 <= count <= planned:
                        result["succeeded"] = count
                except (CommandError, OSError, ValueError, TypeError, KeyError):
                    pass  # старые/неполные артефакты: score и infra-ошибки остаются доступны из лога
            results.append(result)
        if results:
            self.store.save_measurement(run_id, run.sha, measured_at or _iso(self.clock()), results)

    async def restore_measurements(self) -> None:
        for run in self.runs():
            if run.status != "done" or self.store.measurement(run.id):
                continue
            stage = next((s for s in reversed(self.stages(run.id)) if s.stage == "measure"), None)
            if stage and stage.returncode == 0:
                await self.save_measurement(run.id, measured_at=stage.finished_at, read_s3=False)

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
    async def start(self, spec: BenchSpec, *, ref: str, preview: Preview, access: Access | None = None) -> BenchRun:
        async with self._submit_lock:
            return await self._submit(spec, ref=ref, preview=preview, access=access)

    async def _submit(self, spec: BenchSpec, *, ref: str, preview: Preview, access: Access | None) -> BenchRun:
        existing = self.store.get_bench(spec.run_id)
        if existing is not None:
            if access:
                access.require(existing)
            return existing
        if access:
            access.draft(spec.run_id)
        draft = self.store.conn.execute("SELECT sha FROM drafts WHERE id=?", (spec.run_id,)).fetchone()
        if draft and draft["sha"] and draft["sha"] != spec.sha:
            raise BenchError("Ревизия черновика изменилась; соберите новое превью")
        if spec.session:
            session = access.session(spec.session.id) if access else self.sessions.get(spec.session.id)
            if session.status != "ready":
                raise BenchError("сессия модели не готова")
            if session.owner_id != (access.id if access else LOCAL_OWNER):
                raise BenchError("Для прогона нужна сессия того же владельца")
        try:
            root = private_root(self.settings)
        except ValueError as exc:
            raise BenchError(str(exc)) from exc
        spec = replace(spec, isolate_model=True, read_only_measure=True, private_s3_root=root,
                       mlsc_base_url=self.settings.cluster.mlsc_base_url)
        if not supports_private_measure(spec.worktree):
            raise BenchError("Эта ревизия fleet не поддерживает приватный замер read_only; выберите поддерживаемый SHA")
        spec.write()
        resolved, error = await self.print_config(spec)
        if resolved is None:
            raise BenchError(error)
        actual = from_resolved(resolved, revision=preview.revision, context=preview.cluster.split(" · ")[0],
                               run_dir=spec.run_dir, session=spec.session.id if spec.session else "")
        self._validate_resolved(spec, resolved, actual)
        try:
            _, user_limit = self.store.user_limits(access.id if access else LOCAL_OWNER, self.settings.quotas)
            gpu = check_gpu(0 if spec.session else actual.gpu_total, self.settings, user_limit=user_limit)
        except ValueError as exc:
            raise BenchError(str(exc)) from exc
        preview = actual
        now = self.clock()
        run = BenchRun(
            id=spec.run_id, status="queued", created_at=_iso(now), preset=spec.preset, ref=ref, sha=spec.sha,
            worktree=str(spec.worktree), dir=str(spec.run_dir), recipe=spec.recipe, model=spec.effective_model or preview.model,
            agent=spec.agent, dataset=spec.dataset if isinstance(spec.dataset, str) else spec.dataset.model_dump_json(),
            jobs=spec.effective_jobs, agents_per_replica=spec.agents_per_replica, task_limit=spec.task_limit, inference=spec.inference,
            session_id=spec.session.id if spec.session else None, stage="preflight", preview_json=preview.to_json(),
            version_id=spec.session.version_id if spec.session else None,
            owner_id=access.id if access else LOCAL_OWNER,
        )
        if not self.store.enqueue(run, gpu, spec.run_id):
            return self.get(spec.run_id)
        self.store.event("bench_start", run.id, "ok", f"{run.model} × {spec.dataset_label} × {spec.agent}; {run.jobs} × {run.agents_per_replica}")
        await self.dispatch()
        return self.get(run.id)

    def _validate_resolved(self, spec: BenchSpec, resolved: dict, preview: Preview) -> None:
        system = resolved.get("system") or {}
        if preview.run_id != spec.run_id or preview.model != spec.effective_model:
            raise BenchError("Итоговый конфиг меняет ID прогона или маршрут модели")
        if not spec.session and (problem := job_priority_problem(preview.queue, preview.job_priority)):
            raise BenchError(problem)
        if spec.read_only_measure and (system.get("measure_run") or {}).get("read_only") is not True:
            raise BenchError("Итоговый конфиг не подтверждает приватный замер read_only")
        if spec.private_s3_root:
            expected = f"{spec.private_s3_root}/{spec.effective_model}/{spec.run_id}"
            if preview.s3 != expected:
                raise BenchError("Итоговый конфиг меняет закрытый путь результатов")
            # Нельзя объявить приватным префикс в общем бакете результатов Fleet.
            from urllib.parse import urlparse
            if urlparse(spec.private_s3_root).netloc == urlparse(str(system.get("bucket") or "")).netloc:
                raise BenchError("Приватные результаты требуют отдельного закрытого S3-бакета")

    async def dispatch(self) -> None:
        async with self._dispatch_lock:
            for run_id in self.store.queued("bench"):
                run = self.get(run_id)
                try:
                    spec = await self.spec_of(run)
                    if spec.session and self.sessions.get(spec.session.id).status != "ready":
                        raise BenchError("Сессия модели больше не готова; создайте новый прогон")
                except (BenchError, OSError, ValueError) as exc:
                    self.store.update_bench(run_id, status="failed", error=str(exc))
                    self.store.release(run_id)
                    continue
                if self.store.admit(run_id, self.settings.quotas):
                    self.store.update_bench(run_id, started_at=run.started_at or _iso(self.clock()))
                    self._launch(run_id, spec, position(self.plan_of(run), run.stage or "preflight", run.requeues))

    async def retry(self, run_id: str, *, access: Access | None = None) -> BenchRun:
        async with self._actions.setdefault(run_id, asyncio.Lock()):
            return await self._retry(run_id, access=access)

    async def _retry(self, run_id: str, *, access: Access | None = None) -> BenchRun:
        """«Повторить стадию»: с той, на которой упали."""
        run = access.run(run_id) if access else self.get(run_id)
        if run.status not in {"attention", "failed", "stopped"}:
            raise BenchError("повторять можно только упавшую или остановленную стадию")
        if not run.stage:
            raise BenchError("стадия неизвестна")
        spec = await self.spec_of(run)
        request = self.store.request(run_id)
        if request and request["state"] == "released":
            self.store.requeue_request(run_id)
            self.store.update_bench(run_id, status="queued", error="", attention_at=None)
            await self.dispatch()
            return self.get(run_id)
        self.store.update_bench(run_id, status="running", error="", attention_at=None)
        self.store.event("bench_retry", run_id, "ok", run.stage)
        self._launch(run_id, spec, position(self.plan_of(run), run.stage, run.requeues))
        return self.get(run_id)

    async def stop(self, run_id: str, reason: str = "по кнопке", *, access: Access | None = None) -> BenchRun:
        async with self._actions.setdefault(run_id, asyncio.Lock()):
            return await self._stop(run_id, reason, access=access)

    async def _stop(self, run_id: str, reason: str = "по кнопке", *, access: Access | None = None) -> BenchRun:
        """Остановка: убить стадию, лимит 0 → сабмиттер → Terminate workflows → mlsc.stop_llm, если модель своя."""
        run = access.run(run_id) if access else self.get(run_id)
        if run.status == "queued":
            self.store.update_bench(run_id, status="stopped", stage="", finished_at=_iso(self.clock()))
            self.store.release(run_id)
            return self.get(run_id)
        self.store.update_bench(run_id, status="stopping", error="", attention_at=None)
        task = self.tasks.pop(run_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        open_stage = self.store.open_bench_stage(run_id)
        if open_stage is not None:
            files = StageFiles.for_stage(Path(run.dir) / "logs", open_stage.stage)
            try:
                await interrupt_stage(files)
            except RuntimeError as exc:
                self.store.update_bench(run_id, error=str(exc))
                return self.get(run_id)
            self.store.finish_bench_stage(open_stage.id, files.read_exit())
        spec = await self.spec_of(run)
        run = self.get(run_id)
        failed: list[str] = []
        if run.own_model and run.version_id is None and any(s.stage == "create_job" for s in self.stages(run_id)):
            try:
                version_id = mlsc.created_version(await self._configuration(), run_id, spec.read_version_id())
                self.store.update_bench(run_id, version_id=version_id)
                spec.version_id_file.write_text(str(version_id))
                run = self.get(run_id)
            except Exception as exc:
                self.store.update_bench(run_id, error=str(exc))
                return self.get(run_id)
        try:
            report = await self.stop_tasks(run_id)
            failed.extend(getattr(report, "failed", []))
            if getattr(report, "remaining", None):
                failed.append(f"В Argo ещё активны workflows: {report.remaining}")
            if report is not None:
                sub = await self._read_submitter(run_id)
                if sub and sub.alive:
                    failed.append("Сабмиттер прогона ещё работает")
        except Exception as exc:  # noqa: BLE001 — остановку задач показываем, но модель всё равно гасим
            failed.append(f"задачи: {exc}")
        if run.own_model and run.version_id is not None:
            try:
                ok = await self._stage(run_id, spec, "stop_llm")
                if not ok:
                    failed.append("mlsc.stop_llm завершился с ошибкой — модель может быть не погашена")
                elif not mlsc.is_stopped(await self._configuration(), run.version_id):
                    failed.append(f"Версия MLSC {run.version_id} ещё активна; резерв GPU сохранён")
            except Exception as exc:
                failed.append(str(exc))
        status = "stopping" if failed else "stopped"
        self.store.update_bench(run_id, status=status, stage="", finished_at=None if failed else _iso(self.clock()), error="; ".join(failed))
        self.store.event("bench_stop", run_id, "ok" if not failed else "error", reason + ("; " + "; ".join(failed) if failed else ""))
        if not failed:
            self.store.release(run_id)
        return self.get(run_id)

    async def measure(self, run_id: str, *, access: Access | None = None) -> BenchRun:
        async with self._actions.setdefault(run_id, asyncio.Lock()):
            return await self._measure(run_id, access=access)

    async def _measure(self, run_id: str, *, access: Access | None = None) -> BenchRun:
        """Замер по кнопке для остановленного или упавшего прогона: что долетело до S3, то и посчитается."""
        run = access.run(run_id) if access else self.get(run_id)
        if run.active:
            raise BenchError("прогон ещё идёт: замер будет последней стадией")
        spec = await self.spec_of(run)
        previous = run.status
        self.store.update_bench(run_id, status="running", stage="measure")
        ok = await self._stage(run_id, spec, "measure")
        if ok:
            await self.save_measurement(run_id)
        self.store.update_bench(run_id, status="done" if ok else previous, stage="" if ok else "measure", error="" if ok else "замер завершился с ошибкой — см. лог")
        self.store.event("bench_measure", run_id, "ok" if ok else "error")
        return self.get(run_id)

    async def resume(self) -> None:
        """После перезапуска: сверить журнал с диском и кластером, прицепиться к живой стадии или повторить мёртвую."""
        for run in self.runs(active_only=True):
            if run.status == "stopping":
                await self._reconcile_stop(run.id)
            elif run.status == "running":
                await self._resume_run(run.id)

    async def _resume_run(self, run_id: str) -> None:
        async with self._actions.setdefault(run_id, asyncio.Lock()):
            run = self.get(run_id)
            if run.status != "running" or ((task := self.tasks.get(run_id)) and not task.done()):
                return
            try:
                spec = await self.spec_of(run)
                stages = self.plan_of(run)
                open_stage = self.store.open_bench_stage(run_id)
                stage = open_stage.stage if open_stage else run.stage or "preflight"
                index = position(stages, stage, run.requeues)
            except Exception as exc:
                if run.stage in BEFORE_GPU:
                    self.store.update_bench(run.id, status="failed", stage="", error=str(exc))
                else:
                    self._attention(run.id, str(exc))
                return
            if open_stage is None:
                self._launch(run.id, spec, index, interrupted=bool(run.stage))
                return
            files = StageFiles.for_stage(spec.logs_dir, stage)
            if stage not in ("execute", "preflight") and (files.alive() or files.read_exit() is not None):
                self._launch(run.id, spec, index, attach_to=(open_stage.id, files))
                return
            self.store.finish_bench_stage(open_stage.id, None)
            self.store.event("bench_resume", run.id, "ok", f"восстановление прерванной стадии {stage}")
            self._launch(run.id, spec, index, interrupted=True)

    async def _reconcile_stop(self, run_id: str) -> None:
        lock = self._actions.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            run = self.get(run_id)
            if run.status != "stopping":
                return
            stage = self.store.open_bench_stage(run_id)
            if stage is not None:
                files = StageFiles.for_stage(Path(run.dir) / "logs", stage.stage)
                if files.alive():
                    return
                self.store.finish_bench_stage(stage.id, files.read_exit())
            try:
                sub = await self._read_submitter(run_id)
                workflows = await active_workflows(self.runner, self.settings.namespace, run_id)
                remaining = []
                if sub and sub.alive:
                    remaining.append("сабмиттер работает")
                if sub and sub.pending and sub.limit != 0:
                    remaining.append("очередь сабмиттера не заблокирована нулевым лимитом")
                if workflows:
                    remaining.append(f"активные workflows: {len(workflows)}")
                version_id = run.version_id
                if run.own_model and (version_id is not None or any(s.stage == "create_job" for s in self.stages(run_id))):
                    configuration = await self._configuration()
                    if version_id is None:
                        version_id = mlsc.created_version(configuration, run_id)
                        (Path(run.dir) / "version_id").write_text(str(version_id))
                        self.store.update_bench(run_id, version_id=version_id)
                        self.store.adopt_run(run_id, version_id, model=run.model, note="запущен сервисом", owner_id=self.get(run_id).owner_id)
                    if not mlsc.is_stopped(configuration, version_id):
                        remaining.append(f"версия MLSC {version_id} ещё активна")
                if remaining:
                    self.store.update_bench(run_id, error="Остановка прервана: " + ", ".join(remaining))
                    return
            except Exception as exc:
                self.store.update_bench(run_id, error=f"Не удалось сверить остановку: {exc}")
                return
            self.store.update_bench(run_id, status="stopped", stage="", finished_at=_iso(self.clock()), error="")
            self.store.release(run_id)
            self.store.event("bench_stop", run_id, "ok", "после перезапуска кластер подтвердил остановку")

    async def tick(self) -> None:
        """Без ответа в «требует внимания» N минут — гасим модель сами (если включено)."""
        for run in self.runs(active_only=True):
            if run.status == "stopping":
                await self._reconcile_stop(run.id)
        await self.dispatch()
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
    def _launch(self, run_id: str, spec: BenchSpec, index: int, attach_to: tuple[int, StageFiles] | None = None,
                *, interrupted: bool = False) -> None:
        if run_id in self.tasks and not self.tasks[run_id].done():
            return
        self.tasks[run_id] = asyncio.create_task(self._drive(run_id, spec, index, attach_to, interrupted), name=f"bench-{run_id}")

    async def _drive(self, run_id: str, spec: BenchSpec, index: int, attach_to: tuple[int, StageFiles] | None,
                     interrupted: bool) -> None:
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
                if code is None:
                    interrupted = True
                else:
                    skip = stage == "requeue" and not self._requeued(spec)
                    if not await self._after(run_id, spec, stage, code == 0, skip_requeue=skip):
                        return
                    index += 3 if skip else 1
            moved = False
            if (interrupted or attach_to is not None) and index < len(stages):
                self.store.update_bench(run_id, stage=stages[index])
                await self._reconcile_model(self.get(run_id), spec, stages[index])
            if interrupted and stages[index] in {"upload", "requeue"}:
                sub = await self._read_submitter(run_id)
                if (sub and sub.alive) or await active_workflows(self.runner, self.settings.namespace, run_id):
                    raise BenchError("На этапе восстановления обнаружены активные задачи или сабмиттер")
                # Fleet переносит манифесты по одному. Если requeue уже наполнил pending, продолжаем с этой очередью.
                moved = stages[index] == "requeue" and bool(sub and sub.pending)
            while index < len(stages):
                stage = stages[index]
                self.store.update_bench(run_id, stage=stage, status="running")
                if stage == "execute":
                    ok = await self._execute(run_id, spec)
                elif stage == "preflight":
                    resolved, error = await self.print_config(spec, run_id)
                    ok = resolved is not None
                    if ok and self.store.request(run_id):
                        actual = from_resolved(resolved)
                        self._validate_resolved(spec, resolved, actual)
                        if not spec.session and actual.gpu_total != self.store.request(run_id)["gpu"]:
                            raise BenchError("Число GPU изменилось после принятия заявки; создайте новый прогон")
                    if not ok:
                        self.store.update_bench(run_id, error=error)
                elif stage == "launch" and (reason := await self._launch_skip_reason(run_id, recovering=interrupted)):
                    sid = self.store.start_bench_stage(run_id, "launch", f"пропущено: {reason}")
                    self.store.finish_bench_stage(sid, 0)
                    ok = True
                elif stage == "requeue" and moved:
                    sid = self.store.start_bench_stage(run_id, "requeue", "восстановление: манифесты уже в pending")
                    self.store.finish_bench_stage(sid, 0)
                    ok = True
                else:
                    ok = await self._stage(run_id, spec, stage)
                skip = stage == "requeue" and not moved and not self._requeued(spec)
                if not await self._after(run_id, spec, stage, ok, skip_requeue=skip):
                    return
                if skip:
                    index += 2  # нечего перепосылать: launch и execute этого раунда не нужны
                    self.store.event("bench_stage", run_id, "ok", "requeue: переносить нечего, раунд пропущен")
                moved = False
                interrupted = False
                index += 1
            self.store.update_bench(run_id, status="done", stage="", error="", finished_at=_iso(self.clock()))
            self.store.release(run_id)
            self.store.event("bench_done", run_id, "ok")
            await self._notify("Fleet Console", f"Прогон {run_id} завершён: замер готов")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — любая ошибка видна в карточке, а не в stderr сервиса
            self._fail(run_id, self.get(run_id).stage or "прогон", spec, str(exc))

    async def _after(self, run_id: str, spec: BenchSpec, stage: str, ok: bool, *, skip_requeue: bool = False) -> bool:
        """Переход после стадии: version_id после create_job, счётчик requeue; False — прогон остановлен сбоем."""
        if ok and stage == "create_job":
            version_id = spec.read_version_id()
            if version_id is None:
                self._fail(run_id, stage, spec, "mlsc.create_job завершился, но файл version_id пуст")
                return False
            run = self.get(run_id)
            self.store.update_bench(run_id, version_id=version_id)
            self.store.adopt_run(run_id, version_id, model=run.model, note="запущен сервисом", owner_id=run.owner_id)
        if ok and stage == "requeue":
            run = self.get(run_id)
            stages = self.plan_of(run)
            next_index = position(stages, "requeue", run.requeues) + (3 if skip_requeue else 1)
            self.store.update_bench(run_id, requeues=run.requeues + 1, stage=stages[next_index])
        if ok and stage == "stop_llm":
            version_id = self.get(run_id).version_id
            if version_id is None or not mlsc.is_stopped(await self._configuration(), version_id):
                raise BenchError("MLSC ещё не подтвердил освобождение GPU")
        if ok and stage == "measure":
            await self.save_measurement(run_id)
        if not ok:
            self._fail(run_id, stage, spec)
        return ok

    def _requeued(self, spec: BenchSpec) -> bool:
        try:
            text = (spec.logs_dir / "requeue.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        matches = REQUEUED_RE.findall(text.rsplit("\n$ ", 1)[-1])
        return bool(matches) and int(matches[-1][0]) > 0

    async def _reconcile_model(self, run: BenchRun, spec: BenchSpec, stage: str) -> None:
        if stage in (*BEFORE_GPU, "create_job", "measure"):
            return
        version_id = spec.session.version_id if spec.session else run.version_id
        if version_id is None:
            raise BenchError("В журнале нет версии MLSC для возобновления прогона")
        planned, current = mlsc.replica_counts(await self._configuration(), version_id)
        if stage not in {"start_llm", "stop_llm"} and (planned, current) == (0, 0):
            raise BenchError(f"MLSC: версия {version_id} погашена; для стадии {stage} нужна активная модель")
        if not spec.session:
            saved_id = spec.read_version_id()
            if saved_id is not None and saved_id != version_id:
                raise BenchError("Версия MLSC в журнале и в файле прогона расходится")
            if saved_id is None:
                spec.version_id_file.write_text(str(version_id))

    def _fail(self, run_id: str, stage: str, spec: BenchSpec, detail: str = "") -> None:
        tail = detail or _log_tail(spec.logs_dir / f"{stage}.log") or self.get(run_id).error  # у execute лога нет: причина в журнале
        error = f"стадия {stage} не удалась" + (f": {tail}" if tail else "")
        if stage in BEFORE_GPU:
            self.store.update_bench(run_id, status="failed", error=error, finished_at=_iso(self.clock()))
            self.store.release(run_id)
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
        if stage == "create_job" and any(s.stage == "create_job" for s in self.stages(run_id)):
            version_id = mlsc.created_version(await self._configuration(), run_id,
                                              self.get(run_id).version_id or spec.read_version_id())
            spec.version_id_file.write_text(str(version_id))
            self.store.event("bench_reconcile", run_id, "ok", f"найдена версия {version_id}; create_job пропущен")
            return True
        env = self.env_for_fleet()
        if stage == "measure" and spec.read_only_measure:
            env["PYTHONPATH"] = str(spec.run_dir)
            (spec.run_dir / "measurement.json").unlink(missing_ok=True)
        argv = spec.argv(STAGE_STEPS[stage])
        files = StageFiles.for_stage(spec.logs_dir, stage)
        files.clear()
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

    async def _configuration(self) -> dict:
        c = self.settings.cluster
        return await mlsc.get_configuration(self.runner, c.mlsc_namespace, await self.mlsc_pod(), c.mlsc_base_url,
                                           self.secrets().get("MLSC_TOKEN", ""))

    async def _snapshot(self, run_id: str, spec: BenchSpec) -> dict[str, Any]:
        """Свежие данные дашборда: прогон Argo, каталог на сабмиттере, живые реплики модели."""
        obs = self.observer
        while obs.busy:
            await asyncio.sleep(0.01)
        stale = obs.runs.at is None or (datetime.now(timezone.utc) - obs.runs.at).total_seconds() > self.settings.bench.poll_seconds
        # Даже свежий общий снимок мог быть снят до upload/launch этого прогона.
        if stale or not any(s.run_id == run_id for s in obs.submitter.data or []):
            await obs.refresh()
        for source in (obs.runs, obs.submitter, obs.mlsc):
            if source.error or source.data is None:
                raise BenchError("Не удалось получить свежий статус прогона: " + (source.error or "нет данных"))
        argo = next((r for r in obs.runs.data or [] if r.run_name == run_id), None)
        sub = next((s for s in obs.submitter.data or [] if s.run_id == run_id), None)
        run = self.get(run_id)
        version_id = spec.session.version_id if spec.session else run.version_id
        version = next((v for v in obs.mlsc.data or [] if v.version_id == version_id), None) if version_id is not None else None
        return {"argo": argo, "sub": sub, "version": version}

    async def _read_submitter(self, run_id: str) -> submitter.SubmitterRun | None:
        rows = await submitter.list_runs(self.runner, self.settings.namespace, self.settings.cluster.submitter_pod)
        return next((row for row in rows if row.run_id == run_id), None)

    async def _launch_skip_reason(self, run_id: str, *, recovering: bool) -> str:
        # Прямое чтение, без устаревшего кеша observer. Ошибка не означает отсутствие процесса.
        row = await self._read_submitter(run_id)
        if row is None:
            raise BenchError("Состояние submitter не установлено: каталог прогона отсутствует")
        previous = next((s for s in reversed(self.stages(run_id)) if s.stage in {"launch", "requeue"}), None)
        if row.alive:
            return "сабмиттер прогона уже жив на поде"
        previous_launch = previous if previous and previous.stage == "launch" else None
        if recovering and previous_launch and previous_launch.returncode == 0:
            return "запуск сабмиттера уже подтверждён в журнале"
        if recovering or previous_launch:
            if await active_workflows(self.runner, self.settings.namespace, run_id):
                return "задачи прогона уже активны в Argo"
        if previous_launch and previous_launch.returncode != 0:
            raise BenchError("Предыдущий submitter_launch оборвался; отсутствие процесса не подтверждает исход операции")
        return ""

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
