"""Точка входа для Python из worktree. Зависимости Fleet/Harbor принадлежат выбранной ревизии."""

import asyncio
import json
import sys
from glob import has_magic
from pathlib import Path

from fleet.harbor import HarborJobConfigSource
from harbor.models.task.id import LocalTaskId
from harbor.models.task.task import Task
from pydantic import ValidationError


def reason(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        # Pydantic по умолчанию печатает входные значения, в том числе env из JobConfig.
        return "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'config'}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)
        )
    return str(exc)


def invalid_reason(path: Path, disable_verification: bool) -> str:
    if not path.is_dir():
        return "нет каталога задачи"
    if Task.is_valid_dir(path, disable_verification=disable_verification):
        return ""
    # is_valid_dir проглатывает ошибки TaskConfig. Конструктор читает тот же TOML
    # и возвращает подробности ошибки формата или отсутствующего файла verifier.
    try:
        Task(path, disable_verification=disable_verification)
    except (ValueError, OSError) as exc:
        return reason(exc)
    if not (path / "environment").is_dir():
        return "нет каталога environment"
    return "каталог не прошёл Task.is_valid_dir"


async def check_dataset(dataset, disabled: bool) -> int:
    root = dataset.path
    expected = None
    if dataset.is_local():
        # Harbor применяет фильтры и срез n_tasks в порядке iterdir(), после
        # проверки формата. Применяем их же до валидации, чтобы обнаружить подмену.
        candidates = [LocalTaskId(path=p) for p in root.iterdir() if p.is_dir()]
        names = {candidate.get_name() for candidate in candidates}
        for name in dataset.task_names or []:
            if name not in names and not has_magic(name) and Path(name).name == name and name not in {"", ".."}:
                candidates.append(LocalTaskId(path=root / name))
        expected = {task.path for task in dataset._filter_task_ids(candidates)}

    try:
        configs = await dataset.get_task_configs(disable_verification=disabled)
    except ValueError as exc:
        # Если все выбранные каталоги невалидны, сам Harbor может сообщить
        # только «No tasks matched». Восстановим причину для каждого каталога.
        problems = [f"{path}: {why}" for path in sorted(expected or ()) if (why := invalid_reason(path, disabled))]
        if problems:
            raise ValueError("\n".join(problems)) from exc
        raise
    actual = {task.path for task in configs}
    if None in actual:
        raise ValueError("Harbor вернул задачу без локального пути")
    if expected is None:
        for path in actual:
            if problem := invalid_reason(path, disabled):
                raise ValueError(f"{path}: {problem}")
    else:
        problems = [
            f"{path}: {invalid_reason(path, disabled) or 'Harbor пропустил выбранную задачу'}"
            for path in sorted(expected - actual)
        ]
        problems.extend(f"{path}: Harbor выбрал другую задачу" for path in sorted(actual - expected))
        if problems:
            raise ValueError("\n".join(problems))
    if not configs:
        raise ValueError("пустая выборка задач")
    return len(configs)


async def check_materialize(materialize: dict) -> int:
    count = 0
    for entry in [materialize, *(materialize.get("additional_jobs") or [])]:
        job = HarborJobConfigSource.model_validate(entry["job_source"]).load()
        disabled = job.verifier.disable
        if not job.tasks and not job.datasets:
            raise ValueError("пустая выборка задач в JobConfig")
        for task in job.tasks:
            if task.path is None:
                raise ValueError("Harbor вернул задачу без локального пути")
            if problem := invalid_reason(task.path, disabled):
                raise ValueError(f"{task.path}: {problem}")
        count += len(job.tasks)
        for dataset in job.datasets:
            try:
                count += await check_dataset(dataset, disabled)
            except (ValueError, OSError) as exc:
                raise ValueError(f"{dataset.path or dataset.name}: {reason(exc)}") from exc
    return count


if __name__ == "__main__":
    try:
        print(asyncio.run(check_materialize(json.load(sys.stdin))))
    except Exception as error:  # noqa: BLE001 — ошибка любой установленной ревизии должна попасть в preflight
        sys.exit(reason(error))
