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

    def ensure(self) -> "Paths":
        for d in (self.home, self.worktrees, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        return self


class Bastion(BaseModel):
    """ssh-сессия к бастиону: даёт API-сервер на 8443 и SOCKS на 1080 (порты из ~/.ssh/config)."""

    ssh_host: str = "fleetbastion"
    socks_proxy: str = "socks5h://127.0.0.1:1080"


class ArgoForward(BaseModel):
    """Проброс до Argo Server для Argo UI."""

    namespace: str = "argo"
    service: str = "argo-workflows-server"
    remote_port: int = 2746
    local_port: int = 2746


class Settings(BaseModel):
    """Настройки сервиса; файл создаётся с дефолтами при первом запуске."""

    fleet_remote: str = "git@gitverse.ru:gigacode-ai/fleet.git"
    allowed_contexts: list[str] = Field(default_factory=lambda: ["internal"])
    namespace: str = "swe-bench-v2-20260308"
    bastion: Bastion = Field(default_factory=Bastion)
    argo: ArgoForward = Field(default_factory=ArgoForward)
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
