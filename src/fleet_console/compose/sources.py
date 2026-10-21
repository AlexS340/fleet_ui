"""Исходные файлы конфига прогона: дерево defaults с текстом и комментариями, копия под правку, diff.

Правки никогда не пишутся в worktree fleet: «Править» кладёт копию файла в каталог прогона по тому же
относительному пути. Каталог прогона — основной каталог конфигов Hydra для exp.yaml, поэтому копия
перекрывает оригинал только в этом прогоне (проверено на fleet master 2026-10-21).
"""

from __future__ import annotations

from dataclasses import dataclass
import difflib
from pathlib import Path
import re

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


# --- overlay: правка прогона → файл группы сервиса -------------------------------------

OVERLAY_HEADER_RE = re.compile(r"^#\s*fleet-console:\s*из\s+(\S+)\s+ревизии\s+([0-9a-f]{7,40})")
PACKAGE_LINE_RE = re.compile(r"^#\s*@package\s")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


def save_to_overlay(src: SourceFile, overlay: Path, worktree: Path, new_name: str, *, sha: str, run_id: str) -> Path:
    """Копия правленного файла в overlay/<группа>/<имя>.yaml с шапкой о происхождении.

    Имя не должно совпадать с членом группы в ревизии: файл ревизии ищется раньше overlay, и копия
    никогда бы не применилась. Существующий файл overlay не перезаписывается.
    """
    name = new_name.strip()
    if not NAME_RE.match(name):
        raise ValueError("имя: строчные латинские буквы, цифры, «-», «_», «.»")
    if (worktree / CONFIGS_REL / src.group / f"{name}.yaml").is_file():
        raise ValueError(f"в ревизии уже есть {src.group}/{name}.yaml — overlay его не перекроет, выберите другое имя")
    target = overlay / src.group / f"{name}.yaml"
    if target.exists():
        raise ValueError(f"в overlay уже есть {src.group}/{name}.yaml")
    header = f"# fleet-console: из {src.rel} ревизии {sha} · правка прогона {run_id}\n"
    lines = src.text.splitlines(keepends=True)
    text = (lines[0] + header + "".join(lines[1:])) if lines and PACKAGE_LINE_RE.match(lines[0]) else header + src.text
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def overlay_origin(text: str) -> tuple[str, str] | None:
    """(путь оригинала в ревизии, sha) из шапки файла overlay, сохранённого сервисом; иначе None."""
    for line in text.splitlines()[:3]:
        m = OVERLAY_HEADER_RE.match(line)
        if m:
            return m.group(1), m.group(2)
    return None


def strip_overlay_header(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not OVERLAY_HEADER_RE.match(line)) + ("\n" if text.endswith("\n") else "")


@dataclass(frozen=True)
class Drift:
    """Оригинал правленного файла изменился в другой ревизии: три стороны — была, стала, моя правка."""

    rel: str  # файл overlay, напр. harbor/agent/terminus2-mine.yaml
    origin_rel: str  # оригинал в ревизии
    then_sha: str
    diff_original: str  # была → стала
    diff_mine: str  # была → моя правка

    @property
    def summary(self) -> str:
        return f"{self.rel}: оригинал {self.origin_rel} изменился после ревизии {self.then_sha[:8]}"


def three_way(rel: str, origin_rel: str, then_sha: str, then_text: str, now_text: str, mine_text: str) -> Drift | None:
    """None, если оригинал не менялся; иначе оба diff относительно исходной ревизии."""
    if then_text == now_text:
        return None
    return Drift(
        rel, origin_rel, then_sha,
        _diff(then_text, now_text, f"{origin_rel} @{then_sha[:8]}", f"{origin_rel} (текущая ревизия)"),
        _diff(then_text, strip_overlay_header(mine_text), f"{origin_rel} @{then_sha[:8]}", f"{rel} (моя правка)"),
    )


def _diff(a: str, b: str, name_a: str, name_b: str) -> str:
    return "\n".join(difflib.unified_diff(a.splitlines(), b.splitlines(), name_a, name_b, lineterm="", n=2))
