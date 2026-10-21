"""Прогоны Argo: workflows namespace, сгруппированные по лейблу fleet/run-name.

Читается через kubectl с узким jsonpath (как submitter fleet): полный JSON
с status.nodes на сотни workflows весит мегабайты.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fleet_console.observe._time import parse_k8s_time
from fleet_console.runner import CommandError, Runner

NO_RUN = "(без fleet/run-name)"
JSONPATH = (
    'jsonpath={range .items[*]}{.metadata.name}{"\\t"}{.status.phase}{"\\t"}'
    "{.metadata.labels['fleet/run-name']}{\"\\t\"}{.metadata.labels['fleet/model']}{\"\\t\"}"
    '{.metadata.creationTimestamp}{"\\t"}{.status.finishedAt}{"\\n"}{end}'
)


@dataclass(frozen=True)
class Workflow:
    name: str
    phase: str  # Running | Pending | Succeeded | Failed | Error | "" (ещё без фазы)
    run_name: str
    model: str
    created: datetime | None
    finished: datetime | None


@dataclass
class Run:
    """Сводка прогона: счётчики фаз его workflows."""

    run_name: str
    model: str = ""
    phases: Counter = field(default_factory=Counter)
    started: datetime | None = None
    last_finished: datetime | None = None

    @property
    def running(self) -> int:
        return self.phases["Running"]

    @property
    def pending(self) -> int:
        return self.phases["Pending"] + self.phases[""]

    @property
    def succeeded(self) -> int:
        return self.phases["Succeeded"]

    @property
    def failed(self) -> int:
        return self.phases["Failed"] + self.phases["Error"]

    @property
    def active(self) -> int:
        return self.running + self.pending

    @property
    def total(self) -> int:
        return sum(self.phases.values())


def parse(text: str) -> list[Workflow]:
    workflows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = (line.split("\t") + [""] * 6)[:6]
        name, phase, run_name, model, created, finished = parts
        workflows.append(Workflow(name, phase, run_name, model, parse_k8s_time(created), parse_k8s_time(finished)))
    return workflows


def group(workflows: list[Workflow]) -> list[Run]:
    """Прогоны по run-name, новые сверху."""
    runs: dict[str, Run] = {}
    for wf in workflows:
        run = runs.setdefault(wf.run_name or NO_RUN, Run(wf.run_name or NO_RUN))
        run.phases[wf.phase] += 1
        run.model = run.model or wf.model
        if wf.created and (run.started is None or wf.created < run.started):
            run.started = wf.created
        if wf.finished and (run.last_finished is None or wf.finished > run.last_finished):
            run.last_finished = wf.finished
    return sorted(runs.values(), key=lambda r: (r.started is not None, r.started), reverse=True)


async def list_runs(runner: Runner, namespace: str) -> list[Run]:
    result = await runner.run(["kubectl", "-n", namespace, "get", "workflows", "-o", JSONPATH], timeout=60)
    if not result.ok:
        raise CommandError(result)
    return group(parse(result.stdout))


def argo_ui_url(base: str, namespace: str, run_name: str) -> str:
    return f"{base.rstrip('/')}/workflows/{namespace}?label=fleet%2Frun-name%3D{run_name}"


# --- задачи одного прогона ------------------------------------------------------------

TASK_JSONPATH = (
    'jsonpath={range .items[*]}{.metadata.name}{"\\t"}{.status.phase}{"\\t"}'
    "{.metadata.labels['fleet/results-instance']}{\"\\t\"}{.metadata.annotations['fleet/results-rel']}{\"\\t\"}"
    '{.status.startedAt}{"\\t"}{.status.finishedAt}{"\\t"}{.status.message}{"\\n"}{end}'
)
POD_WORKFLOW_LABEL = "workflows.argoproj.io/workflow"


@dataclass(frozen=True)
class TaskWorkflow:
    """Workflow одной задачи: лейбл fleet/results-instance и аннотация fleet/results-rel (папка результатов в S3)."""

    name: str
    phase: str
    instance: str
    results_rel: str
    started: datetime | None
    finished: datetime | None
    message: str = ""

    @property
    def task(self) -> str:
        return self.results_rel.rsplit("/", 1)[-1] or self.instance or self.name

    @property
    def failed(self) -> bool:
        return self.phase in ("Failed", "Error")

    @property
    def active(self) -> bool:
        return self.phase in ("Running", "Pending", "")

    def duration(self, now: datetime) -> timedelta | None:
        if self.started is None:
            return None
        return (self.finished or now) - self.started


def parse_tasks(text: str) -> list[TaskWorkflow]:
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        name, phase, instance, rel, started, finished, message = (line.split("\t") + [""] * 7)[:7]
        out.append(TaskWorkflow(name, phase, instance, rel, parse_k8s_time(started), parse_k8s_time(finished), message))
    return sorted(out, key=lambda t: (t.task, t.started.isoformat() if t.started else ""))


async def list_tasks(runner: Runner, namespace: str, run_name: str) -> list[TaskWorkflow]:
    argv = ["kubectl", "get", "workflows", "-n", namespace, "-l", f"fleet/run-name={run_name}", "-o", TASK_JSONPATH]
    result = await runner.run(argv, timeout=60)
    if not result.ok:
        raise CommandError(result)
    return parse_tasks(result.stdout)


async def pod_logs(runner: Runner, namespace: str, workflow: str, container: str = "main", lines: int = 200) -> str:
    """Хвост лога контейнера пода задачи (пока под жив; в production он удаляется сразу после завершения)."""
    argv = ["kubectl", "logs", "-n", namespace, "-l", f"{POD_WORKFLOW_LABEL}={workflow}", "-c", container, "--tail", str(lines), "--prefix"]
    result = await runner.run(argv, timeout=60)
    if not result.ok:
        raise CommandError(result)
    return result.stdout


def workflow_url(base: str, namespace: str, name: str) -> str:
    return f"{base.rstrip('/')}/workflows/{namespace}/{name}"
