"""Проверка, локальные снимки и публикация своих Harbor-задач в S3."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
import shutil
import tomllib
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import yaml

from fleet_console.compose.task_images import ImageIssue, check_images
from fleet_console.observe.s3 import aws_env
from fleet_console.runner import Runner
from fleet_console.settings import Paths, Settings, aws_secret_names, scrub, secrets_env
from fleet_console.store.db import LOCAL_OWNER


@dataclass
class Validation:
    tasks: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    size: int = 0
    digest: str = ""
    images: dict[str, list[str]] = field(default_factory=dict)
    image_issues: dict[str, ImageIssue] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.tasks) and not self.errors and not any(issue.blocking for issue in self.image_issues.values())


def validate_folder(folder: Path, *, allow_task_links: bool = True, exclude_task: str | None = None) -> Validation:
    """Без запуска кода задач: структура, TOML, образ и входные файлы; один уровень задач."""
    report = Validation()
    digest = hashlib.sha256()
    try:
        folder = folder.expanduser().resolve(strict=True)
        if not folder.is_dir():
            raise ValueError("выберите папку с задачами")
        candidates = [folder] if (folder / "task.toml").is_file() else sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))
        for task in candidates:
            if task.name == exclude_task:
                continue
            if (not allow_task_links and task.is_symlink()) or (task / "task.toml").is_symlink():
                report.errors.append(f"{task.name}: ссылка на папку задачи или task.toml")
                continue
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
                else:
                    report.images.setdefault(image.strip(), []).append(task.name)
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


def validate_import(folder: Path, *, allow_task_links: bool = True) -> Validation:
    report = validate_folder(folder, allow_task_links=allow_task_links)
    if report.ok:
        report.image_issues = check_images(report.images)
    return report


class ImportValidationError(ValueError):
    def __init__(self, report: Validation) -> None:
        self.report = report
        details = [f"{', '.join(report.images[image])}: {issue.message} · {issue.url or image}"
                   for image, issue in report.image_issues.items() if issue.blocking]
        super().__init__("\n".join(report.errors + details))


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
    owner_id: str = LOCAL_OWNER

    @property
    def s3_root(self) -> str:
        return self.s3_uri.rpartition(f"/local/{self.id}/")[0]

    @classmethod
    def read(cls, directory: Path, dataset_id: str) -> LocalDataset:
        if not re.fullmatch(r"local-[a-z0-9-]+", dataset_id):
            raise ValueError("неверный идентификатор набора")
        return cls(**json.loads((directory / f"{dataset_id}.json").read_text(encoding="utf-8")))


class LocalDatasets:
    def __init__(self, runner: Runner, paths: Paths, settings: Settings, overlay: Path, event, *, active_runs) -> None:
        self.runner, self.paths, self.settings, self.overlay, self.event = runner, paths, settings, overlay, event
        self.active_runs = active_runs
        self.directory = paths.home / "local-datasets"
        self.jobs: dict[str, asyncio.Task] = {}

    def all(self) -> list[LocalDataset]:
        records = [LocalDataset(**json.loads(p.read_text(encoding="utf-8"))) for p in self.directory.glob("*.json")]
        return sorted(records, key=lambda item: item.imported_at, reverse=True)

    def get(self, dataset_id: str) -> LocalDataset:
        return LocalDataset.read(self.directory, dataset_id)

    def save(self, dataset: LocalDataset) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{dataset.id}.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(dataset), ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def migrate_owner(self, owner_id: str) -> None:
        for dataset in self.all():
            if dataset.owner_id == LOCAL_OWNER:
                dataset.owner_id = owner_id
                self.save(dataset)

    def mapping(self, dataset: LocalDataset) -> None:
        root = self.settings.datasets_root_path()
        relative = Path(dataset.path).relative_to(root.resolve())
        target = self.overlay / "datasets" / f"{dataset.id}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump({"benchmark": dataset.id, "source": {"tasks_root": relative.as_posix()},
                                        "runtime": {"task_s3_base": dataset.s3_uri or relative.as_posix()}}, sort_keys=False), encoding="utf-8")

    def import_folder(self, folder: Path, name: str, *, owner_id: str = LOCAL_OWNER, allow_task_links: bool = True) -> LocalDataset:
        report = validate_import(folder, allow_task_links=allow_task_links)
        if not report.ok:
            raise ImportValidationError(report)
        root = self.settings.datasets_root_path()
        if root is None:
            raise ValueError("сначала задайте корень датасетов в настройках")
        label = name.strip() or folder.name
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:24] or "tasks"
        dataset_id = f"local-{slug}-{uuid4().hex[:12]}"
        dest = root.expanduser().resolve() / "local" / dataset_id / "tasks"
        warnings = report.warnings + [f"{', '.join(report.images[image])}: {issue.message} · {image}"
                                      for image, issue in report.image_issues.items()]
        dataset = LocalDataset(dataset_id, label, str(folder.expanduser().resolve()), str(dest), datetime.now(UTC).isoformat(),
                               [p.name for p in report.tasks], report.size, warnings, digest=report.digest, owner_id=owner_id)
        try:
            dest.mkdir(parents=True, exist_ok=False)
            for task in report.tasks:
                shutil.copytree(task, dest / task.name, symlinks=True)
            copied = validate_folder(dest)
            if not copied.ok or copied.digest != report.digest or copied.images != report.images:
                raise ValueError("\n".join(copied.errors[:20]) or "исходные файлы изменились во время копирования; повторите импорт")
            self.mapping(dataset)
            self.save(dataset)
        except Exception:
            shutil.rmtree(dest.parent, ignore_errors=True)
            (self.overlay / "datasets" / f"{dataset_id}.yaml").unlink(missing_ok=True)
            raise
        self.event("dataset_import", dataset.id, "ok", f"{dataset.source} → {dataset.path}; задач {len(dataset.tasks)}")
        return dataset

    def deletion_blocker(self, dataset: LocalDataset) -> str:
        job = self.jobs.get(dataset.id)
        if job and not job.done():
            return "Дождитесь завершения отправки в S3."
        snapshot = Path(dataset.path)
        for run in self.active_runs():
            reference = json.loads(run.dataset) if run.dataset.startswith("{") else {}
            if run.dataset == dataset.id or reference.get("mapping") == dataset.id:
                return "Набор используется активным прогоном."
            paths = [reference.get("path"), *(d.get("path") for d in run.preview.get("datasets", []))]
            for path in filter(None, paths):
                # В spec путь относительно корня импорта, в итоговом превью обычно абсолютный.
                if (snapshot.parents[2] / path).resolve().is_relative_to(snapshot):
                    return "Набор используется активным прогоном."
        return ""

    def delete(self, dataset_id: str, *, owner_id: str) -> None:
        dataset = self.get(dataset_id)
        if dataset.owner_id != owner_id:
            raise ValueError("Удалить набор может только его владелец.")
        snapshot = Path(dataset.path)
        if (dataset.id != dataset_id or snapshot.parts[-3:] != ("local", dataset_id, "tasks")
                or snapshot != snapshot.resolve() or Path(dataset.source).resolve().is_relative_to(snapshot)):
            raise ValueError("Не удалось подтвердить путь к локальной копии набора.")
        if reason := self.deletion_blocker(dataset):
            raise ValueError(reason)
        # Запись удаляется последней: при ошибке очистку можно повторить из карточки.
        if snapshot.exists():
            shutil.rmtree(snapshot)
        if snapshot.parent.is_dir() and not any(snapshot.parent.iterdir()):
            snapshot.parent.rmdir()
        (self.overlay / "datasets" / f"{dataset_id}.yaml").unlink(missing_ok=True)
        (self.directory / f"{dataset_id}.json").unlink()
        self.jobs.pop(dataset_id, None)
        self.event("dataset_delete", dataset_id, "ok", f"Владелец: {owner_id}")

    async def remove_task(self, dataset_id: str, task_name: str, *, owner_id: str) -> None:
        dataset = self.get(dataset_id)
        if dataset.owner_id != owner_id:
            raise ValueError("Удалить задачу может только владелец набора.")
        if task_name not in dataset.tasks or Path(task_name).name != task_name or task_name in {"", ".", ".."}:
            raise ValueError("Задачи нет в этом наборе.")
        if len(dataset.tasks) == 1:
            raise ValueError("В наборе должна остаться хотя бы одна задача.")
        root = self.settings.datasets_root_path()
        snapshot = Path(dataset.path)
        if (dataset.id != dataset_id or root is None or snapshot != root.resolve() / "local" / dataset_id / "tasks"
                or snapshot != snapshot.resolve() or Path(dataset.source).resolve().is_relative_to(snapshot)):
            raise ValueError("Не удалось подтвердить путь к локальной копии набора.")
        if reason := self.deletion_blocker(dataset):
            raise ValueError(reason)

        before = await asyncio.to_thread(validate_folder, snapshot, allow_task_links=False)
        if not before.ok or before.digest != dataset.digest:
            raise ValueError("Локальная копия изменилась. Проверьте набор перед удалением.")
        remaining = await asyncio.to_thread(validate_folder, snapshot, allow_task_links=False, exclude_task=task_name)
        if not remaining.ok:
            raise ValueError("\n".join(remaining.errors))
        if self.get(dataset_id) != dataset:
            raise ValueError("Набор изменился. Выберите задачу заново.")
        if reason := self.deletion_blocker(dataset):
            raise ValueError(reason)

        # Сохраняем результат проверки реестра при импорте для оставшихся задач с этим образом.
        for warning in dataset.warnings:
            message, separator, image = warning.rpartition(" · ")
            if separator and image in remaining.images:
                prefix = ", ".join(before.images[image]) + ": "
                if message.startswith(prefix):
                    remaining.warnings.append(f"{', '.join(remaining.images[image])}: {message[len(prefix):]} · {image}")
        updated = replace(dataset, tasks=[p.name for p in remaining.tasks], size=remaining.size,
                          warnings=remaining.warnings, digest=remaining.digest, uploaded_at="", error="")
        if dataset.s3_uri:
            if not dataset.s3_root:
                raise ValueError("Не удалось определить S3-префикс набора.")
            updated.s3_uri = f"{dataset.s3_root}/local/{dataset_id}/revisions/{remaining.digest}/tasks/"
        task = snapshot / task_name
        staged = snapshot.parent / f".remove-{uuid4().hex}"
        task.rename(staged)
        try:
            if dataset.s3_uri:
                self.mapping(updated)
            self.save(updated)
        except Exception:
            staged.rename(task)
            if dataset.s3_uri:
                self.mapping(dataset)
            raise
        self.event("dataset_task_delete", dataset_id, "ok", f"{owner_id}: {task_name}")
        await asyncio.to_thread(shutil.rmtree, staged)

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
        bucket, _, prefix = dataset.s3_uri.removeprefix("s3://").rstrip("/").partition("/")
        prefix += "/"
        # AWS CLI собирает все страницы ListObjectsV2 перед применением JSON-запроса.
        argv = ["aws", "s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix,
                "--output", "json", "--query", "Contents[].Key || `[]`"]
        try:
            result = await self.runner.run(argv, env=env, timeout=120)
        except OSError as exc:
            return f"Не удалось прочитать список объектов S3: {scrub(str(exc), values.values())}"
        if not result.ok:
            detail = (result.stderr or result.stdout).strip() or f"код {result.returncode}"
            return f"Не удалось прочитать список объектов S3: {scrub(detail, values.values())}"
        try:
            keys = json.loads(result.stdout)
        except json.JSONDecodeError:
            keys = None
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            return "AWS CLI вернул некорректный список ключей S3"
        marker = re.compile(re.escape(prefix) + r"([^/]+)/task\.toml")
        published = {match[1] for key in keys if (match := marker.fullmatch(key))}
        expected = set(dataset.tasks)
        problems = []
        if missing := sorted(expected - published):
            problems.append("Отсутствуют в S3: " + ", ".join(missing))
        if extra := sorted(published - expected):
            problems.append("Лишние в S3: " + ", ".join(extra))
        return "\n".join(problems)

    async def _upload(self, dataset: LocalDataset, argv: list[str]) -> None:
        values: dict[str, str] = {}
        try:
            profile = self.settings.s3_write_profile
            values = secrets_env(self.paths, aws_secret_names(profile))
            if len(values) != 2:
                raise ValueError(f"нет ключей профиля S3 {profile}; укажите профиль с правом PutObject в настройках")
            env = aws_env(values, self.paths.home / "aws-write.cfg", self.settings.s3_endpoint, self.settings.s3_region, profile=profile)
            result = await self.runner.run(argv, env=env, timeout=3600)
            log = self.paths.logs / "datasets" / f"{dataset.id}.upload.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(scrub(result.command + "\n" + result.stdout + result.stderr, values.values()), encoding="utf-8")
            if not result.ok:
                output = (result.stderr or result.stdout).strip()
                errors = list(dict.fromkeys(re.findall(r"An error occurred [^\n]+", output)))
                raise ValueError("\n".join(errors[:3]) if errors else output[-2000:] or f"код {result.returncode}")
            dataset.uploaded_at = datetime.now(UTC).isoformat()
        except asyncio.CancelledError:
            dataset.error = "отправка прервана; повторите загрузку"
            raise
        except Exception as exc:  # noqa: BLE001 — сохраняем ошибку фоновой отправки для UI
            dataset.error = scrub(str(exc), values.values())
        finally:
            self.save(dataset)
            self.event("dataset_upload", dataset.id, "error" if dataset.error else "ok", shlex.join(argv) + (f"; {dataset.error}" if dataset.error else ""))
