"""Запись в MLSC: плановое число инстансов версии (реплики, гашение) — как fleet.mlsc.set_planned_state.

Тот же путь, что и чтение в observe.mlsc: curl с пода `litellm-noauth`, токен через stdin.
"""

from __future__ import annotations

import json
import shlex

from fleet_console.runner import CommandError, Runner


def planned_state_argv(namespace: str, pod: str, base_url: str, version_id: int, count: int) -> list[str]:
    url = base_url.rstrip("/") + "/api/v1/service/set_planned_state"
    body = json.dumps({"application_version_planned_state": {str(int(version_id)): int(count)}})
    script = (
        'curl -sSf -k -X POST -H "Authorization: Bearer $(cat)" -H "Content-Type: application/json" '
        f"--data {shlex.quote(body)} {shlex.quote(url)}"
    )
    return ["kubectl", "exec", "-i", "-n", namespace, pod, "--", "sh", "-c", script]


async def set_planned_state(
    runner: Runner, namespace: str, pod: str, base_url: str, token: str, version_id: int, count: int
) -> str:
    """Выставить версии плановое число инстансов; 0 — погасить. Возвращает ответ API."""
    if count < 0:
        raise ValueError(f"число инстансов должно быть ≥ 0, получено {count}")
    result = await runner.run(planned_state_argv(namespace, pod, base_url, version_id, count), stdin=token.strip(), timeout=60)
    if not result.ok:
        raise CommandError(result)
    return result.stdout.strip()
