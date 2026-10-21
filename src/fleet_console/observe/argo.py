"""Прогоны Argo: workflows namespace, сгруппированные по лейблу fleet/run-name.

Читается через kubectl с узким jsonpath (как submitter fleet): полный JSON
с status.nodes на сотни workflows весит мегабайты.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

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
