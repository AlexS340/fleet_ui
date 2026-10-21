"""Лимит агентов, workflows и сабмиттер одного прогона — через kubectl, как это делает fleet.

Лимит: файл `/manifests/harbor_run/<run_id>/agents_limit` на submitter-поде (fleet.operations.agent_limit).
Остановка: лимит 0 → убить сабмиттер прогона (лимит 0 — только пауза, процесс живёт) → Terminate
активным workflows по лейблу `fleet/run-name` (как `terminate_active_workflows` в TUI fleet) → проверка.
Гашение модели прогона — отдельный шаг через MLSC (control.mlsc), если version_id известен.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import shlex

from fleet_console.observe.submitter import ROOT
from fleet_console.runner import CommandError, Runner

ACTIVE_PHASES = {"Running", "Pending", ""}
WF_JSONPATH = 'jsonpath={range .items[*]}{.metadata.name}{"\\t"}{.status.phase}{"\\n"}{end}'
TERMINATE_PATCH = '{"spec":{"shutdown":"Terminate"}}'


@dataclass(frozen=True)
class Command:
    """Что именно выполнили: для карточки прогона и журнала."""

    argv: tuple[str, ...]
    ok: bool
    output: str = ""

    @property
    def text(self) -> str:
        return shlex.join(self.argv)


def limit_argv(namespace: str, pod: str, run_id: str, limit: int) -> list[str]:
    script = f"mkdir -p {ROOT}/{run_id} && printf '%s' '{int(limit)}' > {ROOT}/{run_id}/agents_limit"
    return ["kubectl", "exec", "-n", namespace, pod, "--", "sh", "-c", script]


async def write_agent_limit(runner: Runner, namespace: str, pod: str, run_id: str, limit: int) -> Command:
    if limit < 0:
        raise ValueError(f"лимит агентов должен быть ≥ 0, получено {limit}")
    argv = limit_argv(namespace, pod, run_id, limit)
    result = await runner.run(argv, timeout=30)
    if not result.ok:
        raise CommandError(result)
    return Command(tuple(argv), True)


async def kill_submitter(runner: Runner, namespace: str, pod: str, run_id: str) -> Command:
    """Убить процесс submit_workflows.py этого прогона; если его нет — не ошибка.

    `[.]` в шаблоне: иначе `pkill -f` находит собственный `sh -c`, в чьей командной строке тот же текст,
    и убивает его раньше `true` (код 143) — на живом кластере так и вышло.
    """
    pattern = f"submit_workflows[.]py {ROOT}/{run_id}/"
    argv = ["kubectl", "exec", "-n", namespace, pod, "--", "sh", "-c", f"pkill -f {shlex.quote(pattern)}; true"]
    result = await runner.run(argv, timeout=30)
    if not result.ok:
        raise CommandError(result)
    return Command(tuple(argv), True)


async def active_workflows(runner: Runner, namespace: str, run_id: str) -> list[str]:
    argv = ["kubectl", "-n", namespace, "get", "workflows", "-l", f"fleet/run-name={run_id}", "-o", WF_JSONPATH]
    result = await runner.run(argv, timeout=60)
    if not result.ok:
        raise CommandError(result)
    names = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        name, phase = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
        if name and phase in ACTIVE_PHASES:
            names.append(name)
    return names


async def terminate_workflows(runner: Runner, namespace: str, names: list[str]) -> list[Command]:
    """Terminate каждому workflow; ошибки не прерывают цикл — возвращаются в списке."""
    commands = []
    for name in names:
        argv = ["kubectl", "-n", namespace, "patch", "workflow", name, "--type", "merge", "-p", TERMINATE_PATCH]
        result = await runner.run(argv, timeout=30)
        commands.append(Command(tuple(argv), result.ok, (result.stderr or result.stdout).strip()[-300:]))
    return commands


@dataclass
class StopReport:
    steps: list[str] = field(default_factory=list)  # человекочитаемые строки по шагам
    commands: list[Command] = field(default_factory=list)
    terminated: int = 0
    failed: list[str] = field(default_factory=list)
    remaining: int | None = None

    @property
    def ok(self) -> bool:
        return not self.failed and (self.remaining == 0 or self.remaining is None)


async def stop_run(runner: Runner, namespace: str, submitter_pod: str, run_id: str) -> StopReport:
    """Остановить задачи прогона: лимит 0, сабмиттер, Terminate активным workflows, проверка."""
    report = StopReport()
    try:
        cmd = await write_agent_limit(runner, namespace, submitter_pod, run_id, 0)
        report.commands.append(cmd)
        report.steps.append("лимит агентов = 0")
    except CommandError as exc:
        report.failed.append(f"лимит: {exc}")
    try:
        cmd = await kill_submitter(runner, namespace, submitter_pod, run_id)
        report.commands.append(cmd)
        report.steps.append("сабмиттер прогона остановлен")
    except CommandError as exc:
        report.failed.append(f"сабмиттер: {exc}")
    try:
        names = await active_workflows(runner, namespace, run_id)
    except CommandError as exc:
        report.failed.append(f"список workflows: {exc}")
        return report
    commands = await terminate_workflows(runner, namespace, names)
    report.commands.extend(commands)
    report.terminated = sum(1 for c in commands if c.ok)
    report.failed.extend(f"{c.argv[5]}: {c.output}" for c in commands if not c.ok)
    report.steps.append(f"Terminate отправлен {report.terminated} из {len(names)} активных workflows")
    try:
        report.remaining = len(await active_workflows(runner, namespace, run_id))
        report.steps.append(f"активных workflows после остановки: {report.remaining} (Terminate доходит не мгновенно)")
    except CommandError as exc:
        report.failed.append(f"проверка: {exc}")
    return report
