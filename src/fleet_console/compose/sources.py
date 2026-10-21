"""Исходные файлы конфига прогона: дерево defaults с текстом и комментариями, копия под правку, diff.

Правки никогда не пишутся в worktree fleet: «Править» кладёт копию файла в каталог прогона по тому же
относительному пути. Каталог прогона — основной каталог конфигов Hydra для exp.yaml, поэтому копия
перекрывает оригинал только в этом прогоне (проверено на fleet master 2026-10-21).
"""

from __future__ import annotations

from dataclasses import dataclass
import difflib
from pathlib import Path

import yaml

from fleet_console.fleetref.profile import CONFIGS_REL


@dataclass(frozen=True)
class SourceFile:
    group: str
    name: str
    origin: str  # run | fleet | overlay
    path: Path
    text: str

    @property
    def rel(self) -> str:
        return f"{self.group}/{self.name}.yaml" if self.group else f"{self.name}.yaml"

    @property
    def title(self) -> str:
        return f"{self.rel} · {ORIGIN_TITLES[self.origin]}"


ORIGIN_TITLES = {"run": "правка прогона", "fleet": "ревизия fleet", "overlay": "overlay сервиса"}


def defaults_of(exp_text: str) -> list[tuple[str, str]]:
    """(группа, член) из списка defaults exp.yaml, в порядке применения; `_self_` и `???` пропускаются."""
    try:
        data = yaml.safe_load(exp_text) or {}
    except yaml.YAMLError:
        return []
    out: list[tuple[str, str]] = []
    for entry in data.get("defaults") or [] if isinstance(data, dict) else []:
        if isinstance(entry, dict):
            for key, value in entry.items():
                group = str(key).split("@", 1)[0]
                if value not in (None, "???", "_self_"):
                    out.append((group, str(value)))
        elif isinstance(entry, str) and entry not in ("_self_", "???"):
            group_path = entry.split("@", 1)[0]  # mlsc/common@mlsc → группа mlsc, член common
            group, _, name = group_path.rpartition("/")
            out.append((group, name))
    return out


def locate(group: str, name: str, run_dir: Path, worktree: Path, overlay: Path) -> SourceFile | None:
    rel = Path(group) / f"{name}.yaml" if group else Path(f"{name}.yaml")
    for origin, root in (("run", run_dir), ("fleet", worktree / CONFIGS_REL), ("overlay", overlay)):
        path = root / rel
        if path.is_file():
            return SourceFile(group, name, origin, path, path.read_text(encoding="utf-8", errors="replace"))
    return None


def sources(exp_text: str, run_dir: Path, worktree: Path, overlay: Path) -> list[SourceFile]:
    """Файлы defaults в порядке применения, каждый — из первого каталога, где он есть (прогон → fleet → overlay)."""
    found = []
    for group, name in defaults_of(exp_text):
        src = locate(group, name, run_dir, worktree, overlay)
        if src is not None:
            found.append(src)
    return found


def copy_for_edit(src: SourceFile, run_dir: Path) -> Path:
    """Копия файла группы в каталог прогона; уже скопированный не трогаем."""
    target = run_dir / src.rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(src.text, encoding="utf-8")
    return target


def original_of(src: SourceFile, worktree: Path, overlay: Path) -> SourceFile | None:
    """Оригинал правленного файла: тот же путь в ревизии или overlay."""
    for origin, root in (("fleet", worktree / CONFIGS_REL), ("overlay", overlay)):
        path = root / src.rel
        if path.is_file():
            return SourceFile(src.group, src.name, origin, path, path.read_text(encoding="utf-8", errors="replace"))
    return None


def unified_diff(original: str, edited: str, name: str) -> str:
    lines = difflib.unified_diff(original.splitlines(), edited.splitlines(), f"{name} (оригинал)", f"{name} (прогон)", lineterm="", n=2)
    return "\n".join(lines)


def manual_edits(run_dir: Path, worktree: Path, overlay: Path, exp_text: str) -> list[tuple[str, str]]:
    """(файл, diff) для всех копий групп в каталоге прогона, отличающихся от оригинала."""
    edits: list[tuple[str, str]] = []
    for group, name in defaults_of(exp_text):
        src = locate(group, name, run_dir, worktree, overlay)
        if src is None or src.origin != "run":
            continue
        original = original_of(src, worktree, overlay)
        if original is None:
            edits.append((src.rel, f"новый файл прогона ({len(src.text.splitlines())} строк)"))
        elif original.text != src.text:
            edits.append((src.rel, unified_diff(original.text, src.text, src.rel)))
    return edits


def validate_yaml(text: str) -> str | None:
    """Ошибка разбора YAML с номером строки или None."""
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"строка {mark.line + 1}: " if mark else ""
        return where + str(getattr(exc, "problem", None) or exc).strip()
    return None
