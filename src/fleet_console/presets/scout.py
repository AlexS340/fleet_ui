"""Разведка бенча для нового пресета: что о нём есть в ревизии fleet и какой smoke-пресет из этого собрать.

    uv run python -m fleet_console.presets.scout <бенч> [--ref master] [--model …] [--agent …] [--tasks 3] [--name …] [--write]

Читает зеркало сервиса (`git cat-file`, без worktree и без fetch): группы `harbor/dataset`, реестр `datasets/`,
эксперименты корня configs (прямые пути, группа датасета, сьют) и ветки с именем бенча. Печатает находки,
предупреждения и черновик YAML; `--write` кладёт его в presets/. Навык `.claude/skills/bench-preset` опирается на
этот вывод, чтобы не искать по fleet руками.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from fleet_console.presets import Preset, compatibility
from fleet_console.settings import OVERLAY_DIR, PRESETS_DIR, default_home

CONFIGS = "examples/harbor_new/configs/"
DEFAULT_MODEL = "qwen36_35b_a3b"  # 1 GPU, на нём проверены оба smoke
ROOT_VAR = "${system.datasets_root}/"
UNFILTER = "harbor_job.datasets.0.task_names=[]"  # пустой список для Harbor = без фильтра (проверено на гейте Ф3)
PREFIX_OK = re.compile(r"^[a-z0-9-]+$")  # fleet кладёт benchmark в generate_name workflow без очистки


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _key(text: Any) -> str:
    """cowork-bench, cowork_bench и CoworkBench сравниваются одинаково."""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def tasks_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} задача"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} задачи"
    return f"{n} задач"


def _short(paths: list[str]) -> str:
    return paths[0] + (f" и ещё {len(paths) - 1}" if len(paths) > 1 else "") if paths else "без путей"


def _load(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _dataset_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    items = (data.get("harbor_job") or {}).get("datasets") or []
    return [d for d in items if isinstance(d, dict)]


def _defaults(data: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in data.get("defaults") or []:
        if isinstance(item, dict) and len(item) == 1:
            ((group, member),) = item.items()
            out[str(group).split("@")[0]] = str(member)
    return out


@dataclass
class DatasetGroup:
    name: str
    paths: list[str]
    benchmark: str
    filtered: bool  # в файле есть task_names

    @property
    def problems(self) -> list[str]:
        out = []
        if any(not p.startswith(ROOT_VAR) for p in self.paths):
            out.append(f"путь не под FLEET_DATASETS_ROOT ({_short(self.paths)})")
        if not PREFIX_OK.match(self.benchmark or "-"):
            out.append(f"benchmark «{self.benchmark}» не годится для имени workflow")
        return out


@dataclass
class RegistryEntry:
    name: str
    benchmark: str
    tasks_root: str


@dataclass
class Experiment:
    file: str
    model: str
    agent: str
    datasets: int  # сколько бенчей в эксперименте: чем меньше, тем ближе он к нашему


@dataclass
class Scout:
    query: str
    groups: dict[str, list[str]] = field(default_factory=dict)
    dataset_groups: list[DatasetGroup] = field(default_factory=list)
    entries: list[RegistryEntry] = field(default_factory=list)
    experiments: list[Experiment] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    served: dict[str, str] = field(default_factory=dict)  # mlsc/model → served_model_name


def groups_of(paths: list[str], overlay: Path | None = OVERLAY_DIR) -> dict[str, list[str]]:
    """Группы Hydra ревизии (пути относительно корня репозитория) плюс overlay сервиса."""
    rels = [p[len(CONFIGS) :] for p in paths if p.startswith(CONFIGS)]
    if overlay is not None and overlay.is_dir():
        rels += [p.relative_to(overlay).as_posix() for p in overlay.rglob("*.yaml")]
    groups: dict[str, list[str]] = defaultdict(list)
    for rel in rels:
        if "/" in rel and rel.endswith(".yaml"):
            group, member = rel.rsplit("/", 1)
            if member[:-5] not in groups[group]:
                groups[group].append(member[:-5])
    return dict(groups)


def scout(query: str, files: dict[str, str], branches: list[str], overlay: Path | None = OVERLAY_DIR) -> Scout:
    """`files` — YAML ревизии {путь от корня репозитория: текст}; `branches` — ветки зеркала, свежие первыми."""
    q = _key(query)
    s = Scout(query=query, groups=groups_of(list(files), overlay))

    def hit(*values: Any) -> bool:
        return any(q in _key(v) for v in values if v)

    parsed = {p[len(CONFIGS) :]: _load(t) for p, t in files.items() if p.startswith(CONFIGS) and p.endswith(".yaml")}
    group_paths: dict[str, list[str]] = {}
    suite_paths: dict[str, list[str]] = {}
    for rel, data in sorted(parsed.items()):
        stem = rel.rsplit("/", 1)[-1][:-5]
        paths = [str(d.get("path", "")) for d in _dataset_items(data)]
        if rel.startswith("harbor/dataset/"):
            group_paths[stem] = paths
            # группа на несколько бенчей (служебная или сьют) — кандидат только по своему имени, не по путям внутри
            if hit(stem) or (len(paths) == 1 and hit(paths[0])):
                bench = str((data.get("harbor_dataset") or {}).get("benchmark") or "")
                s.dataset_groups.append(DatasetGroup(stem, paths, bench, any(d.get("task_names") for d in _dataset_items(data))))
        elif rel.startswith("harbor/suite/"):
            suite_paths[stem] = paths
        elif rel.startswith("datasets/"):
            source = data.get("source") or {}
            if hit(stem, data.get("benchmark"), source.get("tasks_root")):
                s.entries.append(RegistryEntry(stem, str(data.get("benchmark") or ""), str(source.get("tasks_root") or "")))
        elif rel.startswith("mlsc/model/") and data.get("served_model_name"):
            s.served[stem] = str(data["served_model_name"])
    for rel, data in sorted(parsed.items()):
        if "/" in rel or "defaults" not in data:
            continue
        defaults = _defaults(data)
        paths = [str(d.get("path", "")) for d in _dataset_items(data)]
        paths += group_paths.get(defaults.get("harbor/dataset", ""), []) + suite_paths.get(defaults.get("harbor/suite", ""), [])
        if any(hit(p) for p in paths) or hit(defaults.get("harbor/dataset")):
            s.experiments.append(Experiment(rel, defaults.get("mlsc/model", ""), defaults.get("harbor/agent", ""), max(len(paths), 1)))
    s.experiments.sort(key=lambda e: (e.datasets, e.file))
    s.branches = [b for b in branches if q in _key(b)]
    return s


def suggest(s: Scout, ref: str = "master", model: str | None = None, agent: str | None = None, tasks: int = 3) -> tuple[dict[str, Any] | None, list[str]]:
    """Черновик smoke-пресета и предупреждения. None — в ревизии нечего запускать."""
    warnings: list[str] = []
    models, agents = s.groups.get("mlsc/model", []), s.groups.get("harbor/agent", [])
    model = model or (DEFAULT_MODEL if DEFAULT_MODEL in models else next(iter(models), DEFAULT_MODEL))
    # агент — из эксперимента fleet с этим бенчем: сначала с той же моделью, потом с меньшим числом бенчей
    ranked = sorted(s.experiments, key=lambda e: (e.model != model, not e.model, e.datasets, e.file))
    agent = agent or next((e.agent for e in ranked if e.agent in agents), None) or ("terminus2" if "terminus2" in agents else next(iter(agents), ""))
    for g in s.dataset_groups:
        if g.problems:
            warnings.append(f"группа harbor/dataset={g.name} не годится: " + "; ".join(g.problems))
    usable = [g for g in s.dataset_groups if not g.problems]
    dataset: str | dict[str, str]
    if usable:
        group = usable[0]
        dataset = group.name
        if group.filtered:
            warnings.append(f"в группе {group.name} фильтр task_names — в пресет добавлен override {UNFILTER}, иначе задач будет меньше {tasks}")
        label = group.name
    elif s.entries:
        entry = s.entries[0]
        if Path(entry.tasks_root).is_absolute():
            warnings.append(f"tasks_root реестра {entry.name} абсолютный: путь под FLEET_DATASETS_ROOT не сработает")
        dataset = {"path": entry.tasks_root, "benchmark": slug(s.query)[:30].rstrip("-") or "bench", "mapping": entry.name}
        label = entry.name
    else:
        return None, warnings + [f"в ревизии нет ни группы harbor/dataset, ни записи реестра datasets для «{s.query}»"]
    covered = {p.removeprefix(ROOT_VAR).strip("/") for g in usable for p in g.paths}
    candidates = [g.name for g in usable] + [e.name for e in s.entries if e.tasks_root.strip("/") not in covered]
    if len(candidates) > 1:
        warnings.append("кандидатов несколько, взят первый (уточните имя бенча): " + ", ".join(candidates))
    if not any(e.agent == agent for e in s.experiments):
        warnings.append(f"агент {agent} не из эксперимента fleet с этим бенчем — проверьте, что бенч с ним работает")
    served = s.served.get(model, "")
    data: dict[str, Any] = {
        "name": f"{model.split('_')[0]}-{slug(s.query)}-smoke",
        "description": f"{served or model} × {label} × {agent}, 1 реплика, {tasks_word(tasks)}",
        "fleet": {"ref": ref},
        "model": model,
        "model_name": f"{served}-console" if served else "",
        "agent": agent,
        "dataset": dataset,
        "resources": {"jobs": 1, "agents_per_replica": tasks},
        "execution": {"profile": "production", "task_limit": tasks},
    }
    if usable and usable[0].filtered:
        data["overrides"] = [UNFILTER]
    warnings += compatibility(Preset.model_validate(data), s.groups, [], [])
    return data, warnings


def render_preset(data: dict[str, Any], s: Scout, ref: str, sha: str) -> str:
    source = f"группа harbor/dataset={data['dataset']}" if isinstance(data["dataset"], str) else f"реестр datasets/{data['dataset']['mapping']}.yaml"
    exp = next((e.file for e in s.experiments if e.agent == data["agent"]), "")
    head = f"# Собран fleet_console.presets.scout из {ref} {sha[:8]}: {source}" + (f", агент из {exp}" if exp else "") + ".\n"
    return head + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=200)


def report(s: Scout, data: dict[str, Any] | None, warnings: list[str], ref: str, sha: str, fetched: str) -> str:
    lines = [f"ревизия {ref} = {sha[:8]} · зеркало обновлено {fetched}"]
    lines.append("harbor/dataset: " + ("; ".join(f"{g.name} (benchmark {g.benchmark}, {_short(g.paths)}{', фильтр task_names' if g.filtered else ''})" for g in s.dataset_groups) or "—"))
    lines.append("реестр datasets: " + ("; ".join(f"{e.name} → {e.tasks_root} (benchmark {e.benchmark})" for e in s.entries) or "—"))
    lines.append("эксперименты: " + ("; ".join(f"{e.file}: {e.model} × {e.agent}, бенчей {e.datasets}" for e in s.experiments[:5]) or "—"))
    lines.append(f"ветки с именем: {len(s.branches)}" + (" · " + ", ".join(s.branches[:6]) if s.branches else ""))
    lines += [f"⚠ {w}" for w in warnings]
    if data is not None:
        lines += ["", f"--- presets/{data['name']}.yaml", render_preset(data, s, ref, sha).rstrip()]
    return "\n".join(lines)


def parse_cat_batch(out: bytes) -> list[str]:
    """Вывод `git cat-file --batch`: `<oid> <type> <size>\\n<содержимое>\\n` подряд."""
    texts, i = [], 0
    while i < len(out):
        end = out.index(b"\n", i)
        size = int(out[i:end].split()[2])
        texts.append(out[end + 1 : end + 1 + size].decode("utf-8", "replace"))
        i = end + 1 + size + 1
    return texts


def read_mirror(mirror: Path, ref: str) -> tuple[str, dict[str, str], list[str], str]:
    def git(*args: str, data: bytes | None = None) -> bytes:
        return subprocess.run(["git", "--git-dir", str(mirror), *args], input=data, capture_output=True, check=True).stdout

    sha = git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    blobs = [line.split("\t", 1) for line in git("ls-tree", "-r", sha, "--", CONFIGS).decode().splitlines()]
    blobs = [(meta.split()[2], path) for meta, path in blobs if path.endswith(".yaml")]
    texts = parse_cat_batch(git("cat-file", "--batch", data="".join(f"{oid}\n" for oid, _ in blobs).encode()))
    branches = git("for-each-ref", "--sort=-committerdate", "--format=%(refname:short)", "refs/heads").decode().split()
    marker = mirror / "fleet-console.last_fetch"
    fetched = datetime.fromtimestamp(marker.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if marker.exists() else "неизвестно когда"
    return sha, {path: text for (_, path), text in zip(blobs, texts, strict=True)}, branches, fetched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fleet_console.presets.scout", description=__doc__.splitlines()[0])
    parser.add_argument("bench", help="имя бенча или его часть: cowork, birdsql, terminal-bench")
    parser.add_argument("--ref", default="master", help="ветка, тег или SHA в зеркале сервиса")
    parser.add_argument("--model", help="mlsc/model; по умолчанию qwen36_35b_a3b")
    parser.add_argument("--agent", help="harbor/agent; по умолчанию из эксперимента fleet с этим бенчем")
    parser.add_argument("--tasks", type=int, default=3, help="лимит задач и агентов на реплику")
    parser.add_argument("--name", help="имя пресета; по умолчанию <модель>-<бенч>-smoke")
    parser.add_argument("--write", action="store_true", help="записать presets/<name>.yaml (не перезаписывает)")
    args = parser.parse_args(argv)
    mirror = default_home() / "fleet.git"
    if not (mirror / "fleet-console.last_fetch").exists():
        print(f"нет готового зеркала {mirror}: запустите сервис и подготовьте ревизию", file=sys.stderr)
        return 2
    try:
        sha, files, branches, fetched = read_mirror(mirror, args.ref)
    except subprocess.CalledProcessError as exc:
        print(f"git: {exc.stderr.decode().strip()}", file=sys.stderr)
        return 2
    s = scout(args.bench, files, branches)
    data, warnings = suggest(s, args.ref, args.model, args.agent, args.tasks)
    if data is not None and args.name:
        data["name"] = args.name
    print(report(s, data, warnings, args.ref, sha, fetched))
    if data is None:
        return 1
    if args.write:
        path = PRESETS_DIR / f"{data['name']}.yaml"
        if path.exists():
            print(f"\n{path} уже есть — не перезаписываю", file=sys.stderr)
            return 1
        path.write_text(render_preset(data, s, args.ref, sha), encoding="utf-8")
        print(f"\nзаписан {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
