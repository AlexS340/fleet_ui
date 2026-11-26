"""Сравнение запросов пода с allocatable одного узла из пула этого workflow."""

import json
from pathlib import Path

from fleet_console.compose.preflight import Check
from fleet_console.connect.kube import KubeStatus
from fleet_console.fleetref.profile import DUMMY_ENV
from fleet_console.observe.nodes import quantity
from fleet_console.runner import Runner

TITLE = "CPU и память задачи"
NODE_FIELDS = (
    'jsonpath={range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\\t"}'
    '{.spec.unschedulable}{"\\t"}{.spec.taints}{"\\t"}{.status.allocatable.cpu}{"\\t"}'
    '{.status.allocatable.memory}{"\\n"}{end}'
)


def tolerated(taint: dict, tolerations: list[dict]) -> bool:
    if taint.get("effect") not in {"NoExecute", "NoSchedule"}:
        return True
    for t in tolerations:
        if t.get("effect") and t["effect"] != taint["effect"]:
            continue
        if t.get("operator") == "Exists" and (not t.get("key") or t["key"] == taint["key"]):
            return True
        if (t.get("operator", "Equal") == "Equal" and t.get("key") == taint["key"]
                and t.get("value", "") == taint.get("value", "")):
            return True
    return False


def compare(report: dict, nodes: str, pool: str) -> Check:
    capacities = []
    for line in nodes.splitlines():
        ready, cordon, taints, cpu, memory = line.split("\t")
        if ready == "True" and cordon != "true" and all(
            tolerated(t, report["tolerations"]) for t in json.loads(taints or "[]")
        ):
            capacities.append((quantity(cpu), quantity(memory)))
    if not capacities:
        return Check("task_resources", TITLE, "warn", f"Пул {pool}: нет готовых узлов, подходящих для размещения.")
    if not report["tasks"]:
        raise ValueError("empty selection")

    oversized = []
    incomplete = False
    peak_cpu = peak_memory = 0
    for path, dind in report["tasks"].items():
        request = []
        for runner_q, dind_q in zip(report["runner"], dind, strict=True):
            a, b = quantity(str(runner_q)), quantity(str(dind_q))
            if a is None or b is None:
                raise ValueError("unknown request")
            request.append(a + b)
        cpu, memory = request
        peak_cpu, peak_memory = max(peak_cpu, cpu), max(peak_memory, memory)
        if any(c is not None and m is not None and c >= cpu and m >= memory for c, m in capacities):
            continue
        if all((c is not None and c < cpu) or (m is not None and m < memory) for c, m in capacities):
            oversized.append(f"{Path(path).name} ({cpu.normalize():f} CPU, {(memory / 2**20).normalize():f} MiB)")
        else:
            incomplete = True

    detail = f"Пул {pool}, подходящих узлов: {len(capacities)}. "
    if oversized:
        detail += "Запрос пода превышает allocatable каждого узла: " + ", ".join(oversized[:10])
        if len(oversized) > 10:
            detail += f" и ещё {len(oversized) - 10}"
        detail += ". "
    if incomplete:
        detail += "Оценка неполная: узлы не сообщили CPU или память."
    elif not oversized:
        detail += (f"Запросы пода: CPU до {peak_cpu:f}, память до {peak_memory / 2**20:f} MiB. "
                   f"Для каждой задачи есть узел с достаточным allocatable.")
    return Check("task_resources", TITLE, "warn" if oversized or incomplete else "ok", detail.strip())


async def task_resources(runner: Runner, worktree: Path, resolved: dict, kube: KubeStatus) -> Check:
    if not (kube.allowed and kube.ready and kube.context):
        return Check("task_resources", TITLE, "warn", "Оценка недоступна: нет подключения к разрешённому кластеру.")
    try:
        system = resolved["system"]
        materialize = system["materialize"]
        batch = system["manifest_runner"]
        builder = batch["workflow_builder"]
        if (materialize.get("_target_") != "fleet.HarborTaskMaterializer"
                or batch.get("_target_") != "fleet.BatchWorkflowRunner"
                or builder.get("_target_") != "fleet.HarborWorkflowBuilder"):
            raise ValueError("unsupported builder")
        probe = Path(__file__).with_name("resource_probe.py")
        result = await runner.run(
            [str(worktree / ".venv/bin/python"), "-I", "-B", str(probe)], cwd=worktree,
            stdin=json.dumps({"materialize": materialize, "config": builder}), env=DUMMY_ENV, timeout=300,
        )
        if not result.ok:
            raise ValueError("probe failed")
        report = json.loads(result.stdout)
        selector = ",".join(f"{k}={v}" for k, v in sorted(report["selector"].items()))
        argv = ["kubectl", "--context", kube.context, "get", "nodes"]
        if selector:
            argv.extend(["-l", selector])
        result = await runner.run([*argv, "-o", NODE_FIELDS], timeout=30)
        if not result.ok:
            return Check("task_resources", TITLE, "warn", "Не удалось прочитать allocatable узлов выбранного пула.")
        return compare(report, result.stdout, selector or "без селектора")
    except (OSError, KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return Check("task_resources", TITLE, "warn", "Оценка недоступна: не удалось определить запросы пода или ресурсы узлов.")
