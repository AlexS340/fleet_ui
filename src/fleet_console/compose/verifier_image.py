"""Чтение /tests/test.sh в локальной копии закреплённого Linux-образа, без запуска его кода."""

import base64
import json
import re
from pathlib import Path
from uuid import uuid4

import httpx


def script_present(image: str) -> bool | None:
    # Кеш тега на машине Console может отличаться от того, что скачает под.
    if not re.fullmatch(r"[\w./:-]+@sha256:[0-9a-f]{64}", image, re.ASCII):
        return None
    socket = next((p for p in (Path("/var/run/docker.sock"), Path.home() / ".docker/run/docker.sock")
                   if p.is_socket()), None)
    if socket is None:
        return None
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket)), base_url="http://localhost", timeout=5) as client:
            return _inspect(client, image)
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
        return None


def _inspect(client: httpx.Client, image: str) -> bool | None:
    # Docker Engine create использует только локальные слои. Контейнер остаётся остановленным.
    name = f"fleet-console-preflight-{uuid4().hex}"
    created = client.post("/containers/create", params={"name": name, "platform": "linux/amd64"},
                          json={"Image": image, "Entrypoint": ["/bin/true"], "NetworkDisabled": True})
    if created.status_code != 201:
        return None
    container = f"/containers/{name}"
    try:
        # GET отличает отсутствие файла от исчезновения контейнера по JSON ошибки.
        # Тело tar с содержимым скрипта читать не требуется.
        with client.stream("GET", container + "/archive", params={"path": "/tests/test.sh"}) as response:
            if response.status_code == 200:
                info = json.loads(base64.b64decode(response.headers["X-Docker-Container-Path-Stat"]))
                # Go FileMode: верхние биты задают тип (в том числе каталог или ссылка).
                return True if info["mode"] >> 16 == 0 else None
            if response.status_code == 404:
                response.read()
                if "Could not find the file /tests/test.sh in container" in response.json().get("message", ""):
                    return False
            return None
    finally:
        client.delete(container, params={"v": "true"}).raise_for_status()
