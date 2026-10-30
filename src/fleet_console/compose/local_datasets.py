"""Проверка, локальные снимки и публикация своих Harbor-задач в S3."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
import shutil
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import yaml

from fleet_console.observe.s3 import aws_env
from fleet_console.runner import Runner
from fleet_console.settings import Paths, Settings, aws_secret_names, scrub, secrets_env


@dataclass
class Validation:
    tasks: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    size: int = 0
    digest: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.tasks) and not self.errors


def validate_folder(folder: Path) -> Validation:
    """Без запуска кода задач: структура, TOML, образ и входные файлы; один уровень задач."""
    report = Validation()
    digest = hashlib.sha256()
    try:
        folder = folder.expanduser().resolve(strict=True)
        if not folder.is_dir():
            raise ValueError("выберите папку с задачами")
        candidates = [folder] if (folder / "task.toml").is_file() else sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))
        for task in candidates:
            if not (task / "task.toml").is_file():
                report.errors.append(f"{task.name}: нет task.toml; выберите папку, непосредственно содержащую задачи (например en или ru)")
                continue
            report.tasks.append(task)
            try:
                config = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))
                environment = config.get("environment", {})
                verifier = config.get("verifier", {})
                if not isinstance(environment, dict) or not isinstance(verifier, dict):
                    raise TypeError("environment и verifier должны быть таблицами TOML")
                image = environment.get("docker_image")
                if not isinstance(image, str) or not image.strip():
                    report.errors.append(f"{task.name}: нужен environment.docker_image — основной образ собирается заранее")
                elif "/" not in image:
                    report.warnings.append(f"Короткое имя образа {image}; проверьте доступность из кластера")
                for rel in ("instruction.md", "tests/test.sh"):
                    if not (task / rel).is_file() or not (task / rel).stat().st_size:
                        report.errors.append(f"{task.name}: нет непустого {rel}")
                if not (task / "environment").is_dir():
                    report.errors.append(f"{task.name}: нет environment/")
                mode = verifier.get("environment_mode")
                if mode not in (None, "shared", "separate"):
                    report.errors.append(f"{task.name}: неизвестный verifier.environment_mode")
                if mode == "separate" or (mode is None and verifier.get("environment")):
                    env = verifier.get("environment") or environment
                    if not isinstance(env, dict):
                        raise ValueError("verifier.environment должен быть таблицей TOML")
                    if not env.get("docker_image") and not (task / "tests/Dockerfile").is_file():
                        report.errors.append(f"{task.name}: отдельному verifier нужен образ или tests/Dockerfile")
                    if not config.get("artifacts"):
                        report.warnings.append(f"{task.name}: separate без artifacts — проверьте передачу результатов")
                for path in sorted(task.rglob("*")):
                    if path.is_symlink():
                        report.errors.append(f"{task.name}: ссылка внутри задачи {path.relative_to(task)}; замените её файлом")
                    elif path.is_file():
                        report.size += path.stat().st_size
                        digest.update(f"{task.name}/{path.relative_to(task)}\0".encode())
                        with path.open("rb") as stream:
                            while chunk := stream.read(1024 * 1024):
                                digest.update(chunk)
                    elif not path.is_dir():
                        report.errors.append(f"{task.name}: специальный файл {path.relative_to(task)}")
            except (OSError, ValueError, TypeError) as exc:
                report.errors.append(f"{task.name}: {exc}")
        if not report.tasks:
            report.errors.append("в папке не найдено задач с task.toml")
    except (OSError, ValueError) as exc:
        report.errors.append(str(exc))
    report.digest = digest.hexdigest()
    report.warnings = list(dict.fromkeys(report.warnings))
    return report


@dataclass
class LocalDataset:
    id: str
    name: str
    source: str
    path: str
    imported_at: str
    tasks: list[str]
    size: int
    warnings: list[str]
    s3_uri: str = ""
    uploaded_at: str = ""
    error: str = ""
    digest: str = ""


class LocalDatasets:
    def __init__(self, runner: Runner, paths: Paths, settings: Settings, overlay: Path, event) -> None:
        self.runner, self.paths, self.settings, self.overlay, self.event = runner, paths, settings, overlay, event
        self.directory = paths.home / "local-datasets"
        self.jobs: dict[str, asyncio.Task] = {}

    def all(self) -> list[LocalDataset]:
        records = [LocalDataset(**json.loads(p.read_text(encoding="utf-8"))) for p in self.directory.glob("*.json")]
        return sorted(records, key=lambda item: item.imported_at, reverse=True)

    def get(self, dataset_id: str) -> LocalDataset:
        if not re.fullmatch(r"local-[a-z0-9-]+", dataset_id):
            raise ValueError("неверный идентификатор набора")
        return LocalDataset(**json.loads((self.directory / f"{dataset_id}.json").read_text(encoding="utf-8")))

    def save(self, dataset: LocalDataset) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{dataset.id}.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(dataset), ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def mapping(self, dataset: LocalDataset) -> None:
        root = self.settings.datasets_root_path()
        relative = Path(dataset.path).relative_to(root.resolve())
        target = self.overlay / "datasets" / f"{dataset.id}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump({"benchmark": dataset.id, "source": {"tasks_root": relative.as_posix()},
                                        "runtime": {"task_s3_base": dataset.s3_uri or relative.as_posix()}}, sort_keys=False), encoding="utf-8")

    def import_folder(self, folder: Path, name: str) -> LocalDataset:
        report = validate_folder(folder)
        if not report.ok:
            raise ValueError("\n".join(report.errors[:20]))
        root = self.settings.datasets_root_path()
        if root is None:
            raise ValueError("сначала задайте корень датасетов в настройках")
        label = name.strip() or folder.name
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:24] or "tasks"
        dataset_id = f"local-{slug}-{uuid4().hex[:12]}"
        dest = root.expanduser().resolve() / "local" / dataset_id / "tasks"
        dataset = LocalDataset(dataset_id, label, str(folder.expanduser().resolve()), str(dest), datetime.now(UTC).isoformat(),
                               [p.name for p in report.tasks], report.size, report.warnings, digest=report.digest)
        try:
            dest.mkdir(parents=True, exist_ok=False)
            for task in report.tasks:
                shutil.copytree(task, dest / task.name, symlinks=True)
            copied = validate_folder(dest)
            if not copied.ok or copied.digest != report.digest:
                raise ValueError("\n".join(copied.errors[:20]) or "исходные файлы изменились во время копирования; повторите импорт")
            self.mapping(dataset)
            self.save(dataset)
        except Exception:
            shutil.rmtree(dest.parent, ignore_errors=True)
            (self.overlay / "datasets" / f"{dataset_id}.yaml").unlink(missing_ok=True)
            raise
        self.event("dataset_import", dataset.id, "ok", f"{dataset.source} → {dataset.path}; задач {len(dataset.tasks)}")
        return dataset

    def upload_argv(self, dataset: LocalDataset, s3_root: str) -> list[str]:
        root = s3_root.strip().rstrip("/")
        if not re.fullmatch(r"s3://[a-zA-Z0-9][a-zA-Z0-9.-]*/[^\s${}]+", root) or ".." in root.split("/"):
            raise ValueError("нужен S3-префикс датасетов вида s3://бакет/папка")
        uri = dataset.s3_uri or f"{root}/local/{dataset.id}/tasks/"
        return ["aws", "s3", "cp", dataset.path + "/", uri, "--recursive", "--no-follow-symlinks"]

    def upload(self, dataset_id: str, s3_root: str) -> asyncio.Task:
        if dataset_id in self.jobs and not self.jobs[dataset_id].done():
            raise ValueError("этот набор уже отправляется в S3")
        dataset = self.get(dataset_id)
        report = validate_folder(Path(dataset.path))
        if not report.ok or report.digest != dataset.digest:
            raise ValueError("локальная копия изменилась или повреждена; импортируйте набор заново")
        argv = self.upload_argv(dataset, s3_root)
        dataset.s3_uri, dataset.uploaded_at, dataset.error = argv[4], "", ""
        self.save(dataset)
        self.mapping(dataset)
        task = asyncio.create_task(self._upload(dataset, argv), name=f"upload-{dataset.id}")
        self.jobs[dataset.id] = task
        return task

    def ready(self, dataset_id: str) -> str:
        dataset = self.get(dataset_id)
        if not dataset.uploaded_at:
            return "отправьте набор в S3 в блоке «Локальные задачи»"
        report = validate_folder(Path(dataset.path))
        if not report.ok or report.digest != dataset.digest:
            return "локальная копия изменилась после загрузки; импортируйте набор заново"
        return ""

    async def remote_ready(self, dataset_id: str) -> str:
        problem = await asyncio.to_thread(self.ready, dataset_id)
        if problem:
            return problem
        dataset = self.get(dataset_id)
        profile = self.settings.s3_read_profile
        values = secrets_env(self.paths, aws_secret_names(profile))
        if len(values) != 2:
            return f"нет ключей профиля S3 {profile} для проверки загруженных задач"
        env = aws_env(values, self.paths.home / "aws-read.cfg", self.settings.s3_endpoint, self.settings.s3_region, profile=profile)
        uri = dataset.s3_uri.rstrip("/") + f"/{dataset.tasks[0]}/task.toml"
        try:
            result = await self.runner.run(["aws", "s3", "ls", uri], env=env, timeout=120)
            if result.ok and result.stdout.strip():
                return ""
            return scrub((result.stderr or result.stdout).strip(), values.values()) or f"в S3 нет {uri}; повторите отправку"
        except OSError as exc:
            return scrub(str(exc), values.values())

    async def _upload(self, dataset: LocalDataset, argv: list[str]) -> None:
        values: dict[str, str] = {}
        try:
            profile = self.settings.s3_write_profile
            values = secrets_env(self.paths, aws_secret_names(profile))
            if len(values) != 2:
                raise ValueError(f"нет ключей профиля S3 {profile}; укажите профиль с правом PutObject в настройках")
            env = aws_env(values, self.paths.home / "aws-write.cfg", self.settings.s3_endpoint, self.settings.s3_region, profile=profile)
            result = await self.runner.run(argv, env=env, timeout=3600)
            if not result.ok:
                raise ValueError((result.stderr or result.stdout or f"код {result.returncode}")[-2000:])
            dataset.uploaded_at = datetime.now(UTC).isoformat()
        except asyncio.CancelledError:
            dataset.error = "отправка прервана; повторите загрузку"
            raise
        except Exception as exc:  # noqa: BLE001 — сохраняем ошибку фоновой отправки для UI
            dataset.error = scrub(str(exc), values.values())
        finally:
            self.save(dataset)
            self.event("dataset_upload", dataset.id, "error" if dataset.error else "ok", shlex.join(argv) + (f"; {dataset.error}" if dataset.error else ""))
