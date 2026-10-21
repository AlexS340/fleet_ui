"""Шлюз моделей: port-forward к litellm или nginx, список моделей, сниппеты.

Проверено 2026-10-21: litellm (`litellm-argo/service/litellm-fleet`) отвечает на `GET /v1/models` без
ключа и отдаёт лимиты контекста; nginx (`vllm-proxy/service/nginx-vllm-proxy-test`) на `/v1/models`
даёт 404 — он маршрутизирует только по имени модели в запросе, его модели берём из ConfigMap upstream.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import socket

import httpx

GATEWAY_KEYS = ("litellm", "nginx")


@dataclass(frozen=True)
class Gateway:
    key: str  # litellm | nginx
    namespace: str
    service: str
    remote_port: int
    local_port: int

    @property
    def name(self) -> str:
        """Имя проброса в супервизоре."""
        return f"gateway-{self.key}"

    @property
    def title(self) -> str:
        return f"шлюз {self.key}"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}/v1"

    @property
    def argv(self) -> list[str]:
        return [
            "kubectl", "-n", self.namespace, "port-forward", f"service/{self.service}", f"{self.local_port}:{self.remote_port}",
        ]


@dataclass(frozen=True)
class Model:
    id: str
    max_input_tokens: int | None = None


def parse_target(text: str) -> tuple[str, str, int]:
    """«namespace/service:port» → (namespace, service, port); порт по умолчанию 80."""
    namespace, _, rest = text.strip().partition("/")
    service, _, port = rest.partition(":")
    if not namespace or not service:
        raise ValueError(f"шлюз задаётся как namespace/service[:port], получено {text!r}")
    return namespace, service, int(port or 80)


def free_port(preferred: int) -> int:
    """Предпочтительный локальный порт, если свободен, иначе любой свободный."""
    for port in (preferred, 0):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return sock.getsockname()[1]
            except OSError:
                continue
    raise OSError("не нашёл свободный локальный порт")


async def list_models(http: httpx.AsyncClient, base_url: str) -> list[Model]:
    response = await http.get(f"{base_url}/models", timeout=10)
    response.raise_for_status()
    data = response.json().get("data") or []
    return [Model(str(m["id"]), m.get("max_input_tokens")) for m in data if m.get("id")]


async def probe(http: httpx.AsyncClient, base_url: str) -> bool:
    """Проброс жив, если шлюз ответил по HTTP: nginx на /v1/models даёт 404, это тоже ответ."""
    try:
        response = await http.get(f"{base_url}/models", timeout=5)
    except httpx.HTTPError:
        return False
    return response.status_code < 500


def snippets(base_url: str, model: str) -> dict[str, str]:
    """Готовые запросы к проброшенному шлюзу: curl и OpenAI SDK."""
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "Привет"}]}, ensure_ascii=False)
    curl = f"curl {base_url}/chat/completions -H 'Content-Type: application/json' -d '{body}'"
    python = (
        "from openai import OpenAI\n\n"
        f'client = OpenAI(base_url="{base_url}", api_key="-")\n'
        "reply = client.chat.completions.create(\n"
        f'    model="{model}",\n'
        '    messages=[{"role": "user", "content": "Привет"}],\n'
        ")\n"
        "print(reply.choices[0].message.content)\n"
    )
    return {"curl": curl, "python": python}
