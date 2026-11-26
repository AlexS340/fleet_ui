"""Пул узлов под бенчи (type=bench): готовность и allocatable, плюс поды в Pending в namespace прогонов."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from fleet_console.runner import CommandError, Runner

NODES_JSONPATH = (
    'jsonpath={range .items[*]}{.metadata.name}{"\\t"}{.spec.unschedulable}{"\\t"}'
    '{.status.conditions[?(@.type=="Ready")].status}{"\\t"}{.status.allocatable.cpu}{"\\t"}'
    '{.status.allocatable.memory}{"\\n"}{end}'
)
QUANTITY = re.compile(r"(?P<num>[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)(?P<suffix>[A-Za-z]*)")
MULTIPLIERS = {
    "": 1,
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
    "E": 10**18,
    "Pi": 2**50,
    "Ei": 2**60,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
}


def quantity(text: str) -> Decimal | None:
    m = QUANTITY.fullmatch(text.strip())
    if m and m["suffix"] in MULTIPLIERS:
        return Decimal(m["num"]) * MULTIPLIERS[m["suffix"]]
    return None


def parse_quantity(text: str) -> float:
    """Kubernetes quantity («95500m», «1456Gi») → число базовых единиц; неизвестный формат → 0."""
    return float(quantity(text) or 0)


@dataclass(frozen=True)
class NodePool:
    total: int
    ready: int
    unschedulable: int
    cpu_cores: float  # allocatable, сумма по готовым узлам
    memory_gib: float
    pending_pods: int = 0


def parse_nodes(text: str, pending_pods: int = 0) -> NodePool:
    total = ready = unschedulable = 0
    cpu = mem = 0.0
    for line in text.splitlines():
        if not line.strip():
            continue
        _, unsched, is_ready, cpu_q, mem_q = (line.split("\t") + [""] * 5)[:5]
        total += 1
        if unsched.strip() == "true":
            unschedulable += 1
        if is_ready.strip() == "True":
            ready += 1
            cpu += parse_quantity(cpu_q)
            mem += parse_quantity(mem_q)
    return NodePool(total, ready, unschedulable, cpu, mem / 2**30, pending_pods)


async def pool_status(runner: Runner, selector: str, namespace: str) -> NodePool:
    nodes = await runner.run(["kubectl", "get", "nodes", "-l", selector, "-o", NODES_JSONPATH], timeout=30)
    if not nodes.ok:
        raise CommandError(nodes)
    pods = await runner.run(
        ["kubectl", "-n", namespace, "get", "pods", "--field-selector=status.phase=Pending", "-o", "name"], timeout=30
    )
    if not pods.ok:
        raise CommandError(pods)
    return parse_nodes(nodes.stdout, len(pods.stdout.split()))
