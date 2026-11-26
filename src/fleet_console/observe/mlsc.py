"""MLSC: приложения и версии через curl с пода (как fleet.mlsc): API виден только из кластера.

Токен уходит в под через stdin `kubectl exec -i` и в argv/логи не попадает.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shlex

from fleet_console.runner import CommandError, Result, Runner

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
    last_deploy_error: str = ""

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


def parse_versions(configuration: dict) -> dict[int, Version]:
    """Все известные версии, включая погашенные и версии с ошибкой старта."""
    states = configuration.get("application_version_states")
    versions = configuration.get("application_versions")
    if not isinstance(states, dict) or not isinstance(versions, dict):
        raise RuntimeError("MLSC не вернула списки версий и их состояний")
    rows = {}
    for version_id in sorted(versions.keys() | states.keys(), key=int):
        state = states.get(version_id) or {}
        v = versions.get(str(version_id)) or {}
        rows[int(version_id)] = Version(
            int(version_id),
            v.get("application_name") or "",
            model_name(v),
            int(state.get("planned") or 0),
            int(state.get("current") or 0),
            v.get("instance_type_name") or "",
            v.get("queue_name") or "",
            v.get("base_image") or "",
            str(v.get("last_deploy_error") or "").strip(),
        )
    return rows


def console_run_id(application: str) -> str:
    name = application.rsplit(" | ", 1)[-1]
    return name.removeprefix("fleet-console ") if name.startswith("fleet-console ") else ""


class NoCreatedVersion(RuntimeError):
    """Свежий список MLSC не содержит версии с точным именем прогона."""


def created_version(configuration: dict, run_id: str, known_id: int | None = None) -> int:
    """Только полное совпадение имени заявки, включая версии с нулевым планом."""
    versions = configuration.get("application_versions")
    if not isinstance(versions, dict):
        raise RuntimeError("MLSC не вернул список версий; исход create_job неизвестен")
    matches = [int(key) for key, version in versions.items()
               if console_run_id(str(version.get("application_name") or "")) == run_id]
    if not matches:
        raise NoCreatedVersion("Исход create_job неоднозначен: MLSC не нашла версию прогона; повтор отключён")
    if len(matches) != 1 or (known_id is not None and matches[0] != known_id):
        raise RuntimeError("Исход create_job неоднозначен: нужна одна подтверждённая версия MLSC; повтор отключён")
    return matches[0]


def replica_counts(configuration: dict, version_id: int) -> tuple[int, int]:
    versions, states = configuration.get("application_versions"), configuration.get("application_version_states")
    if not isinstance(versions, dict) or not isinstance(states, dict) or str(version_id) not in versions:
        raise RuntimeError(f"MLSC не подтвердила состояние версии {version_id}")
    state = states.get(str(version_id), {})
    return int(state.get("planned") or 0), int(state.get("current") or 0)


def is_stopped(configuration: dict, version_id: int) -> bool:
    return replica_counts(configuration, version_id) == (0, 0)


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
    data = _response_json(result, "чтение конфигурации MLSC")
    if not isinstance(data, dict):
        raise RuntimeError("Ответ MLSC не содержит объекта конфигурации")
    return data


def _response_json(result: Result, operation: str) -> object:
    # В обоих потоках может оказаться конфигурация кластера или приватный лог воркера.
    if not result.ok:
        raise RuntimeError(f"{operation} завершилось с ошибкой (код {result.returncode})")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise RuntimeError(f"{operation} завершилось с ошибкой (некорректный JSON)") from None


async def get_last_failed_log(runner: Runner, namespace: str, pod: str, base_url: str, token: str, version_id: int) -> str:
    """Единственный лог, который отдаёт MLSC API: последний упавший воркер версии (как вкладка LLM в TUI fleet)."""
    url = base_url.rstrip("/") + f"/api/v1/service/get_last_failed_log?application_version_id={int(version_id)}"
    script = f'curl -sSf -k -H "Authorization: Bearer $(cat)" {shlex.quote(url)}'
    result = await runner.run(["kubectl", "exec", "-i", "-n", namespace, pod, "--", "sh", "-c", script], stdin=token.strip(), timeout=60)
    data = _response_json(result, "чтение лога MLSC")
    return str((data or {}).get("log") or "") if isinstance(data, dict) else ""
