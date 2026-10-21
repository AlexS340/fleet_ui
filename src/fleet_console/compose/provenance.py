"""Откуда пришло значение в resolved.yaml: файл, строка, комментарии и исходная интерполяция.

Hydra при сборке теряет комментарии, поэтому источник ищется проходом по defaults в порядке Hydra:
exp.yaml → его defaults (каждый файл со своим `# @package` и вложенными defaults, `_self_` на месте)
→ overrides командной строки. Побеждает последний, кто задал ключ. Для `@package _global_` и
списков совпадение проверяется по значению: не сошлось — кандидат помечен «вероятно».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

import yaml

from fleet_console.compose.sources import ORIGIN_TITLES
from fleet_console.fleetref.profile import CONFIGS_REL

PACKAGE_RE = re.compile(r"^#\s*@package\s+(\S+)")
OVERRIDE_ORIGIN = "override"
EXP_ORIGIN = "exp"
TITLES = {**ORIGIN_TITLES, OVERRIDE_ORIGIN: "override командной строки", EXP_ORIGIN: "exp.yaml прогона"}


@dataclass(frozen=True)
class Candidate:
    """Одно определение ключа: где и каким текстом."""

    key: str
    origin: str  # exp | run | fleet | overlay | override
    path: str  # файл или «командная строка»
    line: int  # 1-based; 0 — не применимо
    raw: str  # текст значения как в файле; «» у разделов
    comments: str = ""  # комментарии над ключом
    inline: str = ""  # комментарий в строке ключа
    container: bool = False  # раздел (mapping/список), а не лист

    @property
    def interpolation(self) -> bool:
        return "${" in self.raw

    @property
    def where(self) -> str:
        return f"{TITLES.get(self.origin, self.origin)} · {self.path}" + (f":{self.line}" if self.line else "")


@dataclass
class Provenance:
    key: str
    winner: Candidate | None
    others: list[Candidate] = field(default_factory=list)  # перекрытые определения, в порядке применения
    certain: bool = True  # значение победителя совпало с resolved (или это интерполяция/override)

    @property
    def note(self) -> str:
        if self.winner is None:
            return "в исходных файлах не найдено: значение задано кодом fleet или через @package/списки"
        return "" if self.certain else "вероятно: значение в файле не совпало с итоговым (интерполяция, список или @package)"


# --- разбор одного файла -------------------------------------------------------


def package_of(text: str, group: str) -> str:
    """Пакет файла: `# @package X` в первой строке, иначе группа как путь через точку (`harbor/agent` → `harbor.agent`)."""
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    m = PACKAGE_RE.match(first)
    default = group.replace("/", ".")
    if not m:
        return default
    pkg = m.group(1)
    if pkg == "_global_":
        return ""
    if pkg == "_group_":
        return default
    return pkg


def _comments(lines: list[str], line_index: int) -> tuple[str, str]:
    """Комментарии подряд над строкой и комментарий в самой строке (вне кавычек)."""
    above: list[str] = []
    i = line_index - 1
    while i >= 0 and lines[i].strip().startswith("#"):
        above.insert(0, lines[i].strip().lstrip("#").strip())
        i -= 1
    inline = ""
    if 0 <= line_index < len(lines):
        row = lines[line_index]
        in_quote = ""
        for pos, ch in enumerate(row):
            if ch in "\"'" and not in_quote:
                in_quote = ch
            elif ch == in_quote:
                in_quote = ""
            elif ch == "#" and not in_quote and (pos == 0 or row[pos - 1].isspace()):
                inline = row[pos + 1 :].strip()
                break
    return "\n".join(above), inline


def _raw(node: yaml.Node) -> str:
    if isinstance(node, yaml.ScalarNode):
        return node.value
    if isinstance(node, yaml.SequenceNode):
        return "[]" if not node.value else ""
    return "{}" if not node.value else ""


def _walk(node: yaml.Node, prefix: str, lines: list[str], origin: str, path: str, out: list[Candidate]) -> None:
    if isinstance(node, yaml.MappingNode):
        for key_node, value in node.value:
            key = str(key_node.value)
            dotted = f"{prefix}.{key}" if prefix else key
            if key == "defaults" and not prefix:
                continue  # список defaults — не данные
            container = isinstance(value, (yaml.MappingNode, yaml.SequenceNode)) and bool(value.value)
            comments, inline = _comments(lines, key_node.start_mark.line)
            out.append(Candidate(dotted, origin, path, key_node.start_mark.line + 1, _raw(value), comments, inline, container))
            if container:
                _walk(value, dotted, lines, origin, path, out)
    elif isinstance(node, yaml.SequenceNode):
        for i, value in enumerate(node.value):
            dotted = f"{prefix}[{i}]"
            container = isinstance(value, (yaml.MappingNode, yaml.SequenceNode)) and bool(value.value)
            comments, inline = _comments(lines, value.start_mark.line)
            out.append(Candidate(dotted, origin, path, value.start_mark.line + 1, _raw(value), comments, inline, container))
            if container:
                _walk(value, dotted, lines, origin, path, out)


def candidates_of(text: str, package: str, origin: str, path: str) -> list[Candidate]:
    """Все ключи файла с префиксом пакета; битый YAML — пусто."""
    try:
        node = yaml.compose(text)
    except yaml.YAMLError:
        return []
    if node is None:
        return []
    out: list[Candidate] = []
    _walk(node, package, text.splitlines(), origin, path, out)
    if package:  # сам пакет — раздел, собранный из этого файла
        out.insert(0, Candidate(package, origin, path, 0, "", container=True))
    return out


# --- defaults в порядке Hydra ------------------------------------------------------


def parse_defaults(text: str) -> list[tuple[str, str | None]] | None:
    """[(путь группы/файла, пакет или None), …] из списка defaults; `_self_` — как ("_self_", None). Нет defaults — None."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict) or "defaults" not in data:
        return None
    out: list[tuple[str, str | None]] = []
    for entry in data.get("defaults") or []:
        if isinstance(entry, dict):
            for key, value in entry.items():
                if value in (None, "???"):
                    continue
                group, _, pkg = str(key).partition("@")
                values = value if isinstance(value, list) else [value]
                for v in values:
                    out.append((f"{group}/{v}", pkg or None))
        elif isinstance(entry, str):
            if entry == "_self_":
                out.append(("_self_", None))
                continue
            item, _, pkg = entry.partition("@")
            out.append((item, pkg or None))
    return out


def _resolve_package(explicit: str | None, group: str, parent_package: str) -> str:
    """Пакет из `@…` вложенного default относительно родителя: `_here_` — пакет родителя, `_global_` — корень."""
    if explicit is None:
        return group.replace("/", ".")
    if explicit == "_global_":
        return ""
    if explicit in ("_here_", ""):
        return parent_package
    if explicit.startswith("_global_."):
        return explicit[len("_global_.") :]
    return f"{parent_package}.{explicit}" if parent_package else explicit


@dataclass(frozen=True)
class Layer:
    """Один файл в порядке применения с его пакетом."""

    rel: str  # путь относительно каталога конфигов, напр. harbor/agent/terminus2.yaml
    origin: str
    path: Path
    text: str
    package: str


def _locate(rel: str, run_dir: Path, worktree: Path, overlay: Path) -> tuple[str, Path] | None:
    for origin, root in (("run", run_dir), ("fleet", worktree / CONFIGS_REL), ("overlay", overlay)):
        path = root / rel
        if path.is_file():
            return origin, path
    return None


def layers(exp_text: str, run_dir: Path, worktree: Path, overlay: Path, *, exp_name: str = "exp.yaml") -> list[Layer]:
    """Файлы в порядке применения Hydra: defaults файла (рекурсивно), сам файл на месте `_self_` (или последним)."""
    return _layers_of(exp_text, "", "", EXP_ORIGIN, run_dir / exp_name, run_dir, worktree, overlay, 0)


def _layers_of(
    text: str, group: str, package: str, origin: str, path: Path, run_dir: Path, worktree: Path, overlay: Path, depth: int
) -> list[Layer]:
    me = Layer(f"{group}/{path.stem}.yaml" if group else path.name, origin, path, text, package)
    entries = parse_defaults(text)
    if entries is None or depth > 6:
        return [me]
    out: list[Layer] = []
    placed = False
    for item, explicit in entries:
        if item == "_self_":
            out.append(me)
            placed = True
            continue
        if item.startswith("../") or item.startswith("./"):
            base = Path(group)
            while item.startswith("../"):
                base, item = base.parent, item[3:]
            item = item.removeprefix("./")
            rel_path = (base / item).as_posix().lstrip("/")
        else:
            rel_path = item.lstrip("/")
        child_group = rel_path.rpartition("/")[0]
        found = _locate(f"{rel_path}.yaml", run_dir, worktree, overlay)
        if found is None:
            continue
        child_origin, child_path = found
        child_text = child_path.read_text(encoding="utf-8", errors="replace")
        child_package = package_of(child_text, child_group) if explicit is None else _resolve_package(explicit, child_group, package)
        out.extend(_layers_of(child_text, child_group, child_package, child_origin, child_path, run_dir, worktree, overlay, depth + 1))
    if not placed:
        out.append(me)
    return out


# --- индекс и поиск --------------------------------------------------------------------


OVERRIDE_RE = re.compile(r"^\+?~?([A-Za-z0-9_.\[\]\-/@]+)=(.*)$")


def override_candidates(overrides: list[str]) -> list[Candidate]:
    out = []
    for i, text in enumerate(overrides):
        m = OVERRIDE_RE.match(text.strip())
        if m is None:
            continue
        key, value = m.group(1), m.group(2)
        if key.startswith("hydra."):
            continue
        out.append(Candidate(key, OVERRIDE_ORIGIN, "командная строка", 0, value))
    return out


def build_index(layers_: list[Layer], overrides: list[str]) -> dict[str, list[Candidate]]:
    """Ключ → определения в порядке применения."""
    index: dict[str, list[Candidate]] = {}
    for layer in layers_:
        for c in candidates_of(layer.text, layer.package, layer.origin, str(layer.path)):
            index.setdefault(c.key, []).append(c)
    for c in override_candidates(overrides):
        index.setdefault(c.key, []).append(c)
    return index


def _same(raw: str, value: Any) -> bool:
    if "${" in raw:
        return True
    try:
        parsed = yaml.safe_load(raw) if raw != "" else None
    except yaml.YAMLError:
        parsed = raw
    if parsed == value:
        return True
    return str(parsed) == str(value)


def lookup(key: str, index: dict[str, list[Candidate]], value: Any = None) -> Provenance:
    """Последнее определение ключа; для листа сверяется с итоговым значением."""
    found = index.get(key) or []
    if not found:
        return Provenance(key, None)
    winner = found[-1]
    certain = winner.container or winner.origin == OVERRIDE_ORIGIN or _same(winner.raw, value)
    if not certain:  # может победить другой кандидат с совпавшим значением (напр. @package _global_ в другом файле)
        for c in reversed(found[:-1]):
            if not c.container and _same(c.raw, value) and not c.interpolation:
                return Provenance(key, c, [o for o in found if o is not c], certain=False)
    return Provenance(key, winner, found[:-1], certain=certain)


def flatten(data: Any, prefix: str = "") -> list[tuple[str, Any, bool]]:
    """Все узлы итогового конфига: (ключ, значение, раздел ли) в порядке файла."""
    out: list[tuple[str, Any, bool]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            container = isinstance(v, (dict, list)) and bool(v)
            out.append((key, v, container))
            if container:
                out.extend(flatten(v, key))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            key = f"{prefix}[{i}]"
            container = isinstance(v, (dict, list)) and bool(v)
            out.append((key, v, container))
            if container:
                out.extend(flatten(v, key))
    return out


def value_at(data: Any, key: str) -> Any:
    """Значение по ключу вида a.b[0].c."""
    cur = data
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", key):
        if part.startswith("["):
            idx = int(part[1:-1])
            if not isinstance(cur, list) or idx >= len(cur):
                return None
            cur = cur[idx]
        else:
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
    return cur


def tree_nodes(data: Any, prefix: str = "", limit: int = 4000) -> list[dict[str, Any]]:
    """Узлы для ui.tree: id — ключ, label — `key: value` у листа, `key` у раздела."""
    nodes: list[dict[str, Any]] = []
    items = data.items() if isinstance(data, dict) else enumerate(data) if isinstance(data, list) else []
    for k, v in items:
        key = f"{prefix}.{k}" if (prefix and isinstance(data, dict)) else (f"{prefix}[{k}]" if isinstance(data, list) else str(k))
        name = str(k) if isinstance(data, dict) else f"[{k}]"
        if isinstance(v, (dict, list)) and v:
            nodes.append({"id": key, "label": name, "children": tree_nodes(v, key, limit)})
        else:
            shown = "" if v is None else str(v)
            if len(shown) > 80:
                shown = shown[:77] + "…"
            nodes.append({"id": key, "label": f"{name}: {shown}" if v is not None else f"{name}: null"})
        if len(nodes) >= limit:
            break
    return nodes
