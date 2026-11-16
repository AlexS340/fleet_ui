"""Настройки и пути к данным Fleet Console."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
import configparser
import os
from pathlib import Path
import re
import shlex
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PRESETS_DIR = REPO_ROOT / "presets"  # пресеты бенчей, версионируются в git сервиса
# Свои группы Hydra поверх групп ревизии fleet; FLEET_CONSOLE_OVERLAY — для тестов, чтобы не писать в репозиторий.
OVERLAY_DIR = Path(os.environ.get("FLEET_CONSOLE_OVERLAY") or REPO_ROOT / "overlay" / "configs")
KNOWN_ISSUES = REPO_ROOT / "known_issues.yaml"
# Секреты, которые уходят только в окружение подпроцессов fleet; в exp.yaml, resolved.yaml и логи не попадают.
SECRET_NAMES = ("MLSC_TOKEN", "AWS_ACCESS_KEY_ID_gcvmfleet", "AWS_SECRET_ACCESS_KEY_gcvmfleet")
# Ключи S3 fleet читает как AWS_ACCESS_KEY_ID_<профиль>; сервис берёт их из профиля ~/.aws/credentials.
AWS_SECRET_RE = re.compile(r"^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY)_(.+)$")
AWS_FIELDS = {"ACCESS_KEY_ID": "aws_access_key_id", "SECRET_ACCESS_KEY": "aws_secret_access_key"}

SETTINGS_HEADER = (
    "# Настройки Fleet Console. Правятся здесь или на странице «Настройки».\n"
    "# bastion.ssh_host пустой — сервис не управляет ssh-сессией, только проверяет доступ.\n"
    "# Секреты (MLSC_TOKEN) — в окружении сервиса или в secrets.env рядом с этим файлом.\n"
)


def default_home() -> Path:
    """Каталог данных выбранного экземпляра."""
    if home := os.environ.get("FLEET_CONSOLE_HOME"):
        return Path(home).expanduser().resolve()
    if (REPO_ROOT / "config" / "settings.yaml").is_file():
        return REPO_ROOT / ".fleet-console"
    return Path.home() / ".fleet-console"


@dataclass(frozen=True)
class Paths:
    """Расположение данных сервиса внутри каталога home."""

    home: Path
    config_dir: Path | None = None
    fallback_home: Path | None = None

    @classmethod
    def for_project(cls) -> "Paths":
        home = default_home()
        fallback = home if os.environ.get("FLEET_CONSOLE_HOME") else Path.home() / ".fleet-console"
        return cls(home, REPO_ROOT / "config", fallback)

    def project_file(self, name: str) -> Path | None:
        if self.config_dir is not None:
            path = self.config_dir / name
            if path.is_file():
                return path
        return None

    def ssh_command(self) -> list[str]:
        config = self.project_file("ssh_config")
        return ["ssh", "-F", str(config)] if config else ["ssh"]

    def subprocess_env(self) -> dict[str, str]:
        env = {}
        if kubeconfig := self.project_file("kubeconfig"):
            env["KUBECONFIG"] = str(kubeconfig)
        if self.project_file("ssh_config"):
            env["GIT_SSH_COMMAND"] = shlex.join(self.ssh_command())
        if credentials := self.project_file("aws-credentials"):
            env["AWS_SHARED_CREDENTIALS_FILE"] = str(credentials)
        return env

    @property
    def settings_file(self) -> Path:
        return self.project_file("settings.yaml") or (self.fallback_home or self.home) / "settings.yaml"

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
        return self.project_file("secrets.env") or (self.fallback_home or self.home) / "secrets.env"

    @property
    def runs(self) -> Path:
        """Каталоги прогонов и сессий моделей: конфиг, resolved.yaml, version_id, логи стадий."""
        return self.home / "runs"

    def ensure(self) -> "Paths":
        for d in (self.home, self.worktrees, self.logs, self.runs):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self


class Bastion(BaseModel):
    """ssh-сессия к бастиону: даёт API-сервер на 8443 и SOCKS на 1080 (порты из ~/.ssh/config)."""

    ssh_host: str = "gigacode-bastion"
    socks_proxy: str = "socks5h://127.0.0.1:1080"


class Cluster(BaseModel):
    """Источники дашборда — свойства стенда, как system/cluster.yaml и mlsc/common.yaml в fleet."""

    poll_seconds: int = 30
    mlsc_namespace: str = "vllm-proxy"
    mlsc_deployment: str = "litellm-noauth"  # под ищется по деплойменту: имя с хэшем ReplicaSet протухает
    mlsc_base_url: str = "https://text-coordinator-proxy.sberai-rnd.ru/mlc/"
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


class Auth(BaseModel):
    mode: Literal["local", "test", "shared"] = "local"
    legacy_owner: str = ""
    admins: list[str] = Field(default_factory=list)


class Quotas(BaseModel):
    user_runs: int = Field(default=1, ge=1)
    total_runs: int = Field(default=2, ge=1)
    user_gpus: int = Field(default=8, ge=1)
    total_gpus: int = Field(default=16, ge=1)


class Results(BaseModel):
    # Отдельный закрытый bucket/prefix, недоступный учётным записям общего Benchboard.
    # Права bucket настраивает оператор; Console не меняет IAM/ACL.
    private_s3_root: str = ""


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
    # S3 результатов и датасетов: endpoint и регион как у boto3-клиентов fleet (fleet/s3_upload.py); сервис ходит через `aws` CLI.
    s3_endpoint: str = "https://obs.ru-moscow-1.hc.sbercloud.ru"
    s3_region: str = "ru-moscow-1"
    # Профиль ключей для чтения S3 из сервиса (список и скачивание): AWS_*_<профиль> из окружения, secrets.env или ~/.aws/credentials.
    # Ключ gcvmfleet, которым fleet пишет результаты, может не иметь права ListObjects — тогда укажите профиль только для чтения.
    s3_read_profile: str = "gcvmfleet"
    s3_write_profile: str = "gcvmfleet"  # отправка локальных задач; нужен PutObject
    # Benchboard: прогон после замера виден в каталоге по модели и run_id; прямых ссылок на прогон у него нет.
    benchboard_url: str = "https://fleet-benchboard.sberai-rnd.ru"
    bench: Bench = Field(default_factory=Bench)
    auth: Auth = Field(default_factory=Auth)
    quotas: Quotas = Field(default_factory=Quotas)
    results: Results = Field(default_factory=Results)
    _config_dir: Path | None = PrivateAttr(default=None)

    def datasets_root_path(self) -> Path | None:
        raw = self.datasets_root.strip() or os.environ.get("FLEET_DATASETS_ROOT", "").strip()
        if not raw:
            return None
        path = Path(raw).expanduser()
        if self._config_dir and self.datasets_root.strip():
            return (self._config_dir / path).resolve()
        return path

    @classmethod
    def load(cls, paths: Paths) -> "Settings":
        """Прочитать settings.yaml; если файла нет — записать дефолты."""
        if paths.settings_file.exists():
            data = yaml.safe_load(paths.settings_file.read_text(encoding="utf-8")) or {}
            cluster = data.get("cluster") or {}
            if str(cluster.get("mlsc_base_url", "")).rstrip("/") == "https://text-coordinator-proxy.mlrnd.ru/mlc":
                cluster["mlsc_base_url"] = Cluster().mlsc_base_url
            settings = cls.model_validate(data)
            if paths.project_file("settings.yaml"):
                settings._config_dir = paths.config_dir
            return settings
        settings = cls()
        settings.save(paths)
        return settings

    def save(self, paths: Paths) -> None:
        paths.ensure()
        body = yaml.safe_dump(self.model_dump(), allow_unicode=True, sort_keys=False)
        paths.settings_file.write_text(SETTINGS_HEADER + body, encoding="utf-8")


def read_secret(paths: Paths, name: str, aws_credentials: Path | None = None) -> str | None:
    """Явно заданное окружение, затем файлы проекта или прежние пользовательские файлы."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if (credentials := paths.project_file("aws-credentials")) and AWS_SECRET_RE.fullmatch(name):
        return read_aws_credential(name, credentials)
    secrets_file = paths.secrets_env
    if secrets_file.is_file():
        for raw in secrets_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip().removeprefix("export ")
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip("\"'") or None
    return read_aws_credential(name, aws_credentials)


def read_aws_credential(name: str, path: Path | None = None) -> str | None:
    """AWS_ACCESS_KEY_ID_gcvmfleet → поле aws_access_key_id профиля [gcvmfleet]."""
    match = AWS_SECRET_RE.match(name)
    if match is None:
        return None
    if path is None:
        fallback = os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
        path = Path(fallback).expanduser() if fallback else Path.home() / ".aws" / "credentials"
    if not path.is_file():
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


def aws_secret_names(profile: str) -> tuple[str, str]:
    return f"AWS_ACCESS_KEY_ID_{profile}", f"AWS_SECRET_ACCESS_KEY_{profile}"


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
