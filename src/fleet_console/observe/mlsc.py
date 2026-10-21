"""MLSC: приложения и версии через curl с пода (как fleet.mlsc): API виден только из кластера.

Токен уходит в под через stdin `kubectl exec -i` и в argv/логи не попадает.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shlex

from fleet_console.runner import CommandError, Runner

GPU_RE = re.compile(r"(\d+)gpu")
SERVED_MODEL_NAME = re.compile(r"--served-model-name\s+(\S+)")
MODEL_PATH = re.compile(r"MODEL_PATH=(\S+)")
PODS_JSONPATH = 'jsonpath={range .items[*]}{.metadata.name}{"\\t"}{.status.phase}{"\\n"}{end}'


@dataclass(frozen=True)
class Version:
    version_id: int
    application: str
    model: str
    planned: int
    current: int
    instance_type: str = ""
    queue: str = ""
    image: str = ""

    @property
    def gpu_per_instance(self) -> int | None:
        m = GPU_RE.search(self.instance_type)
        return int(m.group(1)) if m else None

    @property
    def gpus(self) -> int | None:
        """GPU, занятые фактически поднятыми инстансами."""
        per = self.gpu_per_instance
        return None if per is None else per * self.current


def model_name(version: dict) -> str:
    """Имя модели из команды джобы: served-model-name, MODEL_NAME или MODEL_PATH (как scripts/mlsc_queue.py fleet)."""
    command = version.get("job_command_line") or ""
    env = version.get("job_env_vars") or {}
    if m := SERVED_MODEL_NAME.search(command):
        return m.group(1)
    if env.get("MODEL_NAME"):
        return str(env["MODEL_NAME"])
    if m := MODEL_PATH.search(command):
        return m.group(1).rstrip("/").rsplit("/", 1)[-1]
    return ""


def active_versions(configuration: dict) -> list[Version]:
    """Версии с planned > 0 или current > 0, по возрастанию id."""
    states = configuration.get("application_version_states") or {}
    versions = configuration.get("application_versions") or {}
    rows = []
    for version_id, state in states.items():
        planned, current = int(state.get("planned") or 0), int(state.get("current") or 0)
        if planned == 0 and current == 0:
            continue
        v = versions.get(str(version_id)) or {}
        rows.append(
            Version(
                int(version_id),
                v.get("application_name") or "",
                model_name(v),
                planned,
                current,
                v.get("instance_type_name") or "",
                v.get("queue_name") or "",
                v.get("base_image") or "",
            )
        )
    return sorted(rows, key=lambda r: r.version_id)


async def find_pod(runner: Runner, namespace: str, deployment: str) -> str:
    """Живой под деплоймента: имя в fleet захардкожено с хэшем ReplicaSet, после редеплоя оно протухает."""
    sel = await runner.run(
        ["kubectl", "-n", namespace, "get", "deploy", deployment, "-o", "jsonpath={.spec.selector.matchLabels}"], timeout=20
    )
    if not sel.ok:
        raise CommandError(sel)
    labels = json.loads(sel.stdout or "{}")
    selector = ",".join(f"{k}={v}" for k, v in labels.items())
    pods = await runner.run(["kubectl", "-n", namespace, "get", "pods", "-l", selector, "-o", PODS_JSONPATH], timeout=20)
    if not pods.ok:
        raise CommandError(pods)
    for line in pods.stdout.splitlines():
        name, _, phase = line.partition("\t")
        if phase.strip() == "Running":
            return name
    raise RuntimeError(f"у деплоймента {namespace}/{deployment} нет Running-пода")


async def get_configuration(runner: Runner, namespace: str, pod: str, base_url: str, token: str) -> dict:
    url = base_url.rstrip("/") + "/api/v1/service/get_configuration"
    script = f'curl -sSf -k -H "Authorization: Bearer $(cat)" {shlex.quote(url)}'
    result = await runner.run(
        ["kubectl", "exec", "-i", "-n", namespace, pod, "--", "sh", "-c", script], stdin=token.strip(), timeout=60
    )
    if not result.ok:
        raise CommandError(result)
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"MLSC вернул не JSON: {result.stdout[:200]!r}") from exc


async def get_last_failed_log(runner: Runner, namespace: str, pod: str, base_url: str, token: str, version_id: int) -> str:
    """Единственный лог, который отдаёт MLSC API: последний упавший воркер версии (как вкладка LLM в TUI fleet)."""
    url = base_url.rstrip("/") + f"/api/v1/service/get_last_failed_log?application_version_id={int(version_id)}"
    script = f'curl -sSf -k -H "Authorization: Bearer $(cat)" {shlex.quote(url)}'
    result = await runner.run(["kubectl", "exec", "-i", "-n", namespace, pod, "--", "sh", "-c", script], stdin=token.strip(), timeout=60)
    if not result.ok:
        raise CommandError(result)
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"MLSC вернул не JSON: {result.stdout[:200]!r}") from exc
    return str((data or {}).get("log") or "") if isinstance(data, dict) else ""
