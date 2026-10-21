"""Сканирование examples/harbor_new/configs: группы, файлы, шапки-комментарии."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import yaml

from fleet_console.fleetref.profile import CONFIGS_REL

ROOT_GROUP = "experiments"
RULER = re.compile(r"^#\s*[=\-#]{3,}\s*$")
PACKAGE = re.compile(r"^#\s*@package\s+(\S+)")
# Порядок показа: сначала то, что выбирают в форме, потом остальное.
GROUP_TITLES: dict[str, str] = {
    "mlsc/model": "Модели · mlsc/model",
    "harbor/agent": "Агенты · harbor/agent",
    "harbor/dataset": "Датасеты · harbor/dataset",
    "datasets": "Реестр датасетов · datasets",
    "mode": "Режимы · mode",
    "execution": "Профили исполнения · execution",
    "system": "Стенд · system",
    "mlsc": "MLSC общее · mlsc",
    "harbor/job": "Скелет JobConfig · harbor/job",
    "harbor/source": "Источник JobConfig · harbor/source",
    "harbor/suite": "Сьюты · harbor/suite",
    "harbor/jobs": "Готовые JobConfig · harbor/jobs",
    "harbor/additional_job": "Доп. задачи · harbor/additional_job",
    ROOT_GROUP: "Эксперименты · корень configs",
}


@dataclass(frozen=True)
class CatalogEntry:
    group: str
    name: str
    path: str  # относительно worktree, posix
    header: str  # очищенная шапка без директивы и линеек
    package: str | None  # значение `# @package`, если есть
    summary: str  # одна строка для списка


def header_comment(text: str) -> tuple[str, str | None]:
    """Ведущие `#`-строки до первой не-комментарной; без `# @package` и линеек."""
    lines: list[str] = []
    package: str | None = None
    for raw in text.splitlines():
        if not raw.startswith("#"):
            break
        if match := PACKAGE.match(raw):
            package = match.group(1)
            continue
        if RULER.match(raw):
            continue
        lines.append(raw[1:].removeprefix(" ").rstrip())
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines), package


def dataset_summary(text: str) -> str:
    """Для реестра datasets/ шапок нет — берём benchmark и tasks_root из тела."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return ""
    if not isinstance(data, dict):
        return ""
    parts = [str(data.get("benchmark", ""))]
    source = data.get("source")
    if isinstance(source, dict) and source.get("tasks_root"):
        parts.append(str(source["tasks_root"]))
    return " · ".join(p for p in parts if p)


def scan(worktree: Path) -> list[CatalogEntry]:
    configs = worktree / CONFIGS_REL
    if not configs.is_dir():
        return []
    entries: list[CatalogEntry] = []
    for file in sorted(configs.rglob("*.yaml")):
        rel_dir = file.parent.relative_to(configs).as_posix()
        group = ROOT_GROUP if rel_dir == "." else rel_dir
        text = file.read_text(encoding="utf-8", errors="replace")
        header, package = header_comment(text)
        summary = dataset_summary(text) if group == "datasets" else _first_line(header)
        entries.append(
            CatalogEntry(
                group=group,
                name=file.stem,
                path=file.relative_to(worktree).as_posix(),
                header=header,
                package=package,
                summary=summary,
            )
        )
    entries.sort(key=lambda e: (group_order([e.group])[0][1], e.name))
    return entries


def group_order(groups: list[str]) -> list[tuple[str, int]]:
    """(группа, ранг): известные группы в порядке GROUP_TITLES, остальные после, по алфавиту."""
    known = list(GROUP_TITLES)
    ranked = []
    for g in groups:
        rank = known.index(g) if g in known else len(known) + sorted(set(groups) - set(known)).index(g)
        ranked.append((g, rank))
    return ranked


def _first_line(header: str, limit: int = 140) -> str:
    for line in header.splitlines():
        if line.strip():
            return line.strip() if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return ""
