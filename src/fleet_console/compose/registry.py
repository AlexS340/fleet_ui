"""Объединённый реестр датасетов: datasets/ ревизии + datasets/ overlay → <каталог прогона>/registry.

Формат файла — как у HarborDatasetRegistry fleet: benchmark, source.tasks_root, runtime.task_s3_base,
priority. Конфликт имён или одинаковый tasks_root в двух файлах — ошибка preflight.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

import yaml

from fleet_console.fleetref.profile import CONFIGS_REL


@dataclass(frozen=True)
class Mapping:
    name: str
    benchmark: str
    tasks_root: str
    task_s3_base: str
    priority: str
    origin: str  # fleet | overlay
    path: str


def read_mapping(path: Path, origin: str) -> Mapping | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
    tasks_root = str(source.get("tasks_root") or "").strip()
    s3_base = str(runtime.get("task_s3_base") or "").strip()
    benchmark = str(data.get("benchmark") or "").strip()
    if not (tasks_root and s3_base and benchmark):
        return None  # fleet такой файл тоже пропускает
    return Mapping(path.stem, benchmark, tasks_root, s3_base, str(data.get("priority", "")), origin, str(path))


def scan(directory: Path, origin: str) -> list[Mapping]:
    if not directory.is_dir():
        return []
    found = [read_mapping(p, origin) for p in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))]
    return [m for m in found if m is not None]


def build_registry(worktree: Path, overlay: Path, run_dir: Path) -> tuple[list[Mapping], list[str]]:
    """Собрать registry/ прогона заново; вернуть маппинги и ошибки (коллизии имён и tasks_root)."""
    target = run_dir / "registry"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    fleet_dir = worktree / CONFIGS_REL / "datasets"
    overlay_dir = overlay / "datasets"
    errors: list[str] = []
    mappings: list[Mapping] = []
    names: dict[str, str] = {}
    roots: dict[str, str] = {}
    for directory, origin in ((fleet_dir, "fleet"), (overlay_dir, "overlay")):
        for m in scan(directory, origin):
            if m.name in names:
                errors.append(f"реестр datasets: имя {m.name} есть и в {names[m.name]}, и в {origin} — переименуйте файл overlay")
                continue
            if m.tasks_root in roots:
                errors.append(f"реестр datasets: tasks_root {m.tasks_root} и у {roots[m.tasks_root]}, и у {m.name}")
                continue
            names[m.name] = origin
            roots[m.tasks_root] = m.name
            shutil.copyfile(m.path, target / Path(m.path).name)
            mappings.append(m)
    return mappings, errors


def find_mapping(mappings: list[Mapping], datasets_root: Path, task_path: Path) -> Mapping | None:
    """Самое длинное совпадение tasks_root с путём датасета — как HarborDatasetRegistry.resolve_mapping."""
    best: Mapping | None = None
    best_len = -1
    task = task_path.expanduser().resolve()
    for m in mappings:
        root = Path(m.tasks_root)
        root = root.resolve() if root.is_absolute() else (datasets_root / m.tasks_root).resolve()
        if task == root or root in task.parents:
            if len(str(root)) > best_len:
                best, best_len = m, len(str(root))
    return best
