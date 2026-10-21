"""Настройки сервиса и пути к данным в ~/.fleet-console."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
import configparser
import os
from pathlib import Path
import re

from pydantic import BaseModel, Field
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PRESETS_DIR = REPO_ROOT / "presets"  # пресеты бенчей, версионируются в git сервиса
OVERLAY_DIR = REPO_ROOT / "overlay" / "configs"  # свои группы Hydra поверх групп ревизии fleet
KNOWN_ISSUES = REPO_ROOT / "known_issues.yaml"
# Секреты, которые уходят только в окружение подпроцессов fleet; в exp.yaml, resolved.yaml и логи не попадают.
SECRET_NAMES = ("MLSC_TOKEN", "AWS_ACCESS_KEY_ID_gcvmfleet", "AWS_SECRET_ACCESS_KEY_gcvmfleet")
# Ключи S3 fleet читает как AWS_ACCESS_KEY_ID_<профиль>; сервис берёт их из профиля ~/.aws/credentials.
AWS_CREDENTIALS = Path("~/.aws/credentials").expanduser()
AWS_SECRET_RE = re.compile(r"^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY)_(.+)$")
AWS_FIELDS = {"ACCESS_KEY_ID": "aws_access_key_id", "SECRET_ACCESS_KEY": "aws_secret_access_key"}

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

    @property
    def runs(self) -> Path:
        """Каталоги прогонов и сессий моделей: конфиг, resolved.yaml, version_id, логи стадий."""
        return self.home / "runs"

    def ensure(self) -> "Paths":
        for d in (self.home, self.worktrees, self.logs, self.runs):
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


class Bench(BaseModel):
    """Бенч-прогоны: опрос, requeue, автогашение без ответа."""

    poll_seconds: int = 20  # цикл исполнения: лимит агентов, счётчики, завершение
    requeue_rounds: int = 1  # раундов system.requeue_failed после первого прохода
    attention_minutes: int = 30  # «требует внимания» без ответа → автогашение модели; 0 — не гасить
    startup_grace_seconds: int = 90  # после submitter_launch: сабмиттер стартует асинхронно, workflows появляются не сразу


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
    # Сессии моделей: время жизни по умолчанию и льготный интервал после уведомления до гашения.
    session_ttl_minutes: int = 120
    session_grace_minutes: int = 10
    # Локальный корень датасетов Harbor (FLEET_DATASETS_ROOT для fleet); пусто — из окружения сервиса.
    datasets_root: str = ""
    bench: Bench = Field(default_factory=Bench)

    def datasets_root_path(self) -> Path | None:
        raw = self.datasets_root.strip() or os.environ.get("FLEET_DATASETS_ROOT", "").strip()
        return Path(raw).expanduser() if raw else None

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


def read_secret(paths: Paths, name: str, aws_credentials: Path = AWS_CREDENTIALS) -> str | None:
    """Секрет из окружения сервиса, иначе из secrets.env (NAME=value, можно с export), для ключей S3 — из ~/.aws/credentials."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if paths.secrets_env.is_file():
        for raw in paths.secrets_env.read_text(encoding="utf-8").splitlines():
            line = raw.strip().removeprefix("export ")
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip("\"'") or None
    return read_aws_credential(name, aws_credentials)


def read_aws_credential(name: str, path: Path = AWS_CREDENTIALS) -> str | None:
    """AWS_ACCESS_KEY_ID_gcvmfleet → поле aws_access_key_id профиля [gcvmfleet] в ~/.aws/credentials."""
    match = AWS_SECRET_RE.match(name)
    if match is None or not path.is_file():
        return None
    field, profile = AWS_FIELDS[match.group(1)], match.group(2)
    parser = configparser.RawConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error:
        return None
    if not parser.has_section(profile):
        return None
    return parser.get(profile, field, fallback="").strip() or None


def secrets_env(paths: Paths, names: Iterable[str] = SECRET_NAMES) -> dict[str, str]:
    """Известные секреты, которые есть: окружение подпроцессов fleet."""
    env = {}
    for name in names:
        value = read_secret(paths, name)
        if value:
            env[name] = value
    return env


def scrub(text: str, secrets: Iterable[str]) -> str:
    """Вырезать значения секретов из текста перед записью на диск или показом в UI."""
    for value in secrets:
        if value and len(value) >= 6:
            text = text.replace(value, "<secret>")
    return text
