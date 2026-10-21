"""Шлюз nginx: сколько реплик каждой модели реально в маршруте (ConfigMap upstream'ов). Регулярки — как в fleet.nginx."""

from __future__ import annotations

import re

from fleet_console.runner import CommandError, Runner

UPSTREAM_BLOCK_RE = re.compile(r"upstream\s+(?P<name>[^\s{]+)\s*\{(?P<body>.*?)\}", re.MULTILINE | re.DOTALL)
UPSTREAM_SERVER_LINE_RE = re.compile(r"^\s*server\s+", re.MULTILINE)


def parse_upstreams(conf: str) -> dict[str, int]:
    """upstream-блок → число server-строк."""
    return {m.group("name"): len(UPSTREAM_SERVER_LINE_RE.findall(m.group("body"))) for m in UPSTREAM_BLOCK_RE.finditer(conf)}


async def model_replicas(runner: Runner, namespace: str, configmap: str, key: str) -> dict[str, int]:
    jsonpath = "{.data." + key.replace(".", r"\.") + "}"
    result = await runner.run(["kubectl", "-n", namespace, "get", "configmap", configmap, "-o", f"jsonpath={jsonpath}"], timeout=20)
    if not result.ok:
        raise CommandError(result)
    if not result.stdout:
        raise RuntimeError(f"ConfigMap {namespace}/{configmap} не содержит ключ {key}")
    return parse_upstreams(result.stdout)
