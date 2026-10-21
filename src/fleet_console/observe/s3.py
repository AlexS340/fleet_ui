"""S3 прогона через `aws` CLI: список объектов, текст файла, статусы задач по папкам tryN.

Ключи — те же, что у fleet (AWS_*_gcvmfleet), endpoint и virtual-адресация — как у boto3-клиентов
fleet (`fleet/s3_upload.py`); они пишутся в отдельный AWS_CONFIG_FILE сервиса, чтобы команда не
зависела от профилей пользователя и повторялась в терминале как есть.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import re
import shlex

from fleet_console.runner import CommandError, Runner

LS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\d+)\s+(.+)$")
TRY_RE = re.compile(r"^try(\d+)$")
TEXT_SUFFIXES = (".log", ".txt", ".json", ".yaml", ".yml", ".md", ".toml", ".sh", ".py")
FLAGS = {"COMPLETED": "completed", "FAILED": "failed"}


@dataclass(frozen=True)
class S3Object:
    key: str  # относительно префикса прогона
    size: int
    modified: str

    @property
    def is_text(self) -> bool:
        return self.key.endswith(TEXT_SUFFIXES) or self.key.rsplit("/", 1)[-1] in FLAGS


@dataclass
class TaskResult:
    """Результаты одной задачи в S3: `<benchmark>/<agent>/results/<task>-<повтор>/tryN/…`."""

    rel: str
    tries: int = 0  # попыток (папок tryN)
    status: str = "no_attempt"  # completed | failed | no_flag | no_attempt — как scan_run_statuses fleet
    files: list[S3Object] = field(default_factory=list)  # файлы последней попытки

    @property
    def task(self) -> str:
        return self.rel.rsplit("/", 1)[-1]


def aws_env(secrets: Mapping[str, str], config_file: Path, endpoint: str, region: str, profile: str = "gcvmfleet") -> dict[str, str]:
    """Окружение для `aws`: ключи из AWS_*_<профиль>, свой config с endpoint и virtual-адресацией."""
    text = f"[default]\nregion = {region}\nendpoint_url = {endpoint}\ns3 =\n  addressing_style = virtual\n"
    try:
        current = config_file.read_text(encoding="utf-8")
    except OSError:
        current = ""
    if current != text:
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(text, encoding="utf-8")
    env = {"AWS_CONFIG_FILE": str(config_file), "AWS_PROFILE": "default"}
    key, secret = secrets.get(f"AWS_ACCESS_KEY_ID_{profile}", ""), secrets.get(f"AWS_SECRET_ACCESS_KEY_{profile}", "")
    if key and secret:
        env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"] = key, secret
    return env


def parse_ls(text: str, prefix: str) -> list[S3Object]:
    """`aws s3 ls --recursive`: дата время размер ключ; ключ — полный, обрезаем до префикса прогона."""
    out = []
    for line in text.splitlines():
        m = LS_RE.match(line.strip())
        if not m:
            continue
        key = m.group(3)
        if prefix and key.startswith(prefix):
            key = key[len(prefix) :]
        key = key.lstrip("/")
        if key:
            out.append(S3Object(key, int(m.group(2)), m.group(1)))
    return out


def _bucket_key(uri: str) -> tuple[str, str]:
    rest = uri.removeprefix("s3://")
    bucket, _, key = rest.partition("/")
    return bucket, key.strip("/")


async def ls(runner: Runner, uri: str, env: Mapping[str, str]) -> list[S3Object]:
    """Все объекты под префиксом (uri со слэшем на конце — только это дерево)."""
    uri = uri.rstrip("/") + "/"
    result = await runner.run(["aws", "s3", "ls", "--recursive", uri], env=env, timeout=120)
    if not result.ok:
        raise CommandError(result)
    _, key = _bucket_key(uri)
    return parse_ls(result.stdout, key + "/" if key else "")


async def cat(runner: Runner, uri: str, env: Mapping[str, str], max_bytes: int = 200_000) -> str:
    """Начало текстового объекта: `aws s3 cp <uri> - | head -c N`."""
    script = f"aws s3 cp {shlex.quote(uri)} - | head -c {int(max_bytes)}"
    result = await runner.run(["sh", "-c", script], env=env, timeout=120)
    if not result.ok:
        raise CommandError(result)
    text = result.stdout
    return text + ("\n… (обрезано)" if len(text.encode("utf-8", "replace")) >= max_bytes else "")


def scan_results(objects: list[S3Object]) -> dict[str, TaskResult]:
    """Задачи по ключам `<benchmark>/<agent>/results/<task>/…`: число tryN, флаг последней попытки, её файлы."""
    tasks: dict[str, TaskResult] = {}
    last_try: dict[str, int] = {}
    per_try: dict[tuple[str, int], list[S3Object]] = {}
    flags: dict[tuple[str, int], str] = {}
    for obj in objects:
        parts = obj.key.split("/")
        if len(parts) < 5 or parts[2] != "results":
            continue
        rel = "/".join(parts[:4])
        task = tasks.setdefault(rel, TaskResult(rel))
        m = TRY_RE.match(parts[4])
        if m is None:
            continue
        n = int(m.group(1))
        last_try[rel] = max(last_try.get(rel, 0), n)
        per_try.setdefault((rel, n), []).append(S3Object("/".join(parts[5:]), obj.size, obj.modified))
        if len(parts) == 6 and parts[5] in FLAGS:
            flags[(rel, n)] = FLAGS[parts[5]]
    for rel, task in tasks.items():
        n = last_try.get(rel, 0)
        task.tries = len({k[1] for k in per_try if k[0] == rel})
        task.files = sorted(per_try.get((rel, n), []), key=lambda o: o.key)
        task.status = flags.get((rel, n), "no_flag" if n else "no_attempt")
    return tasks


def last_try(objects: list[S3Object], rel: str) -> int:
    """Номер последней попытки задачи по ключам; 0 — попыток нет."""
    best = 0
    for obj in objects:
        parts = obj.key.split("/")
        if len(parts) >= 5 and "/".join(parts[:4]) == rel and (m := TRY_RE.match(parts[4])):
            best = max(best, int(m.group(1)))
    return best
