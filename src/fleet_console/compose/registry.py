"""Объединённый реестр датасетов: datasets/ ревизии + datasets/ overlay → <каталог прогона>/registry.

Формат файла — как у HarborDatasetRegistry fleet: benchmark, source.tasks_root, runtime.task_s3_base,
priority. Конфликт имён или одинаковый tasks_root в двух файлах — ошибка preflight.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
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


# --- выбор датасета в форме ----------------------------------------------------------
ROOT_VAR = "${system.datasets_root}/"


def list_mappings(worktree: Path, overlay: Path) -> list[Mapping]:
    """Записи реестра ревизии и overlay без копирования; при совпадении имени или tasks_root первой идёт ревизия."""
    out: list[Mapping] = []
    seen: set[str] = set()
    for m in scan(worktree / CONFIGS_REL / "datasets", "fleet") + scan(overlay / "datasets", "overlay"):
        if m.name not in seen and m.tasks_root not in seen:
            seen |= {m.name, m.tasks_root}
            out.append(m)
    return out


def group_tasks_roots(config_dirs: list[Path]) -> dict[str, str]:
    """Группы harbor/dataset на один каталог под корнем датасетов: имя → tasks_root.

    `config_dirs` — в порядке поиска Hydra (ревизия раньше overlay): одноимённый файл дальше по списку не читается.
    Группа на несколько бенчей или с абсолютным путём (`/data/<user>/…`) запись реестра не заменяет.
    """
    roots: dict[str, str] = {}
    seen: set[str] = set()
    for directory in config_dirs:
        for path in sorted((directory / "harbor" / "dataset").glob("*.yaml")):
            if path.stem in seen:
                continue
            seen.add(path.stem)
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            items = ((data.get("harbor_job") or {}).get("datasets") or []) if isinstance(data, dict) else []
            paths = [str(i.get("path", "")) for i in items if isinstance(i, dict)]
            if len(paths) == 1 and paths[0].startswith(ROOT_VAR):
                roots[path.stem] = paths[0][len(ROOT_VAR) :].strip("/")
    return roots


def registry_choices(mappings: list[Mapping], group_roots: dict[str, str]) -> list[Mapping]:
    """Записи реестра, каталог которых не закрыт группой harbor/dataset: форма предлагает их путём. Группа важнее."""
    covered = set(group_roots.values())
    return [m for m in mappings if m.tasks_root.strip("/") not in covered and not Path(m.tasks_root).is_absolute()]


def bench_slug(text: str) -> str:
    """benchmark для пути из реестра: строчные латинские буквы, цифры и дефис."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "bench"
