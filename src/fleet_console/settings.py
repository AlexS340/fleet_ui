"""Настройки сервиса и пути к данным в ~/.fleet-console."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from pydantic import BaseModel, Field
import yaml

SETTINGS_HEADER = (
    "# Настройки Fleet Console. Правятся здесь или на странице «Настройки».\n"
    "# bastion.ssh_host пустой — сервис не управляет ssh-сессией, только проверяет доступ.\n"
    "# Секреты (MLSC_TOKEN) — в окружении сервиса или в secrets.env рядом с этим файлом.\n"
)


def default_home() -> Path:
    """Каталог данных: $FLEET_CONSOLE_HOME или ~/.fleet-console."""
    return Path(os.environ.get("FLEET_CONSOLE_HOME", "~/.fleet-console")).expanduser()


@dataclass(frozen=True)
class Paths:
    """Расположение данных сервиса внутри каталога home."""

    home: Path

    @property
    def settings_file(self) -> Path:
        return self.home / "settings.yaml"

    @property
    def mirror(self) -> Path:
        return self.home / "fleet.git"

    @property
    def worktrees(self) -> Path:
        return self.home / "worktrees"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def db(self) -> Path:
        return self.home / "console.sqlite"

    @property
    def secrets_env(self) -> Path:
        """KEY=value построчно; запасной источник токенов, когда их нет в окружении сервиса."""
        return self.home / "secrets.env"

    def ensure(self) -> "Paths":
        for d in (self.home, self.worktrees, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        return self


class Bastion(BaseModel):
    """ssh-сессия к бастиону: даёт API-сервер на 8443 и SOCKS на 1080 (порты из ~/.ssh/config)."""

    ssh_host: str = "fleetbastion"
    socks_proxy: str = "socks5h://127.0.0.1:1080"


class Cluster(BaseModel):
    """Источники дашборда — свойства стенда, как system/cluster.yaml и mlsc/common.yaml в fleet."""

    poll_seconds: int = 30
    mlsc_namespace: str = "vllm-proxy"
    mlsc_deployment: str = "litellm-noauth"  # под ищется по деплойменту: имя с хэшем ReplicaSet протухает
    mlsc_base_url: str = "https://text-coordinator-proxy.mlrnd.ru/mlc/"
    nginx_configmap: str = "nginx-model-upstreams-runtime"
    nginx_key: str = "model-upstreams.conf"
    submitter_pod: str = "fleet-submitter-python"
    node_selector: str = "type=bench"
    # Шлюзы моделей для проброса порта: namespace/service:port. litellm — маршрут fleet по умолчанию
    # (`http://litellm.litellm-argo`, тот же пул подов); деплоймент nginx-vllm-proxy в 0, работает -test.
    litellm_gateway: str = "litellm-argo/litellm-fleet:80"
    nginx_gateway: str = "vllm-proxy/nginx-vllm-proxy-test:80"
    gateway_ports: dict[str, int] = Field(default_factory=lambda: {"litellm": 18080, "nginx": 18081})


class Settings(BaseModel):
    """Настройки сервиса; файл создаётся с дефолтами при первом запуске."""

    fleet_remote: str = "git@gitverse.ru:gigacode-ai/fleet.git"
    allowed_contexts: list[str] = Field(default_factory=lambda: ["internal"])
    namespace: str = "swe-bench-v2-20260308"
    bastion: Bastion = Field(default_factory=Bastion)
    # База ссылок на Argo UI (ingress, открывается через SOCKS бастиона); проброс сервис не держит.
    argo_ui_url: str = "https://fleet-argo.sberai-rnd.ru"
    cluster: Cluster = Field(default_factory=Cluster)
    auto_fetch_minutes: int = 0
    worktree_ttl_days: int = 14
    ui_port: int = 8080

    @classmethod
    def load(cls, paths: Paths) -> "Settings":
        """Прочитать settings.yaml; если файла нет — записать дефолты."""
        if paths.settings_file.exists():
            data = yaml.safe_load(paths.settings_file.read_text(encoding="utf-8")) or {}
            return cls.model_validate(data)
        settings = cls()
        settings.save(paths)
        return settings

    def save(self, paths: Paths) -> None:
        paths.ensure()
        body = yaml.safe_dump(self.model_dump(), allow_unicode=True, sort_keys=False)
        paths.settings_file.write_text(SETTINGS_HEADER + body, encoding="utf-8")


def read_secret(paths: Paths, name: str) -> str | None:
    """Секрет из окружения сервиса, иначе из secrets.env (строка NAME=value, можно с export)."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if not paths.secrets_env.is_file():
        return None
    for raw in paths.secrets_env.read_text(encoding="utf-8").splitlines():
        line = raw.strip().removeprefix("export ")
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip("\"'") or None
    return None
