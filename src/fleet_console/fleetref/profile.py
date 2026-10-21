"""Профиль возможностей ревизии fleet: что в ней есть и чего не хватает."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
from typing import Any

import yaml

from fleet_console.fleetref.sync import fleet_argv
from fleet_console.runner import Runner

CONFIGS_REL = Path("examples/harbor_new/configs")
TEMPLATE = "experiment_template.yaml"
REQUIRED_GROUPS = ("mlsc/model", "harbor/agent", "harbor/dataset")
# --print-config делает resolve=True: без этих переменных oc.env падает.
DUMMY_ENV = {
    "FLEET_DATASETS_ROOT": "/nonexistent/fleet-console-datasets",
    "MLSC_TOKEN": "fleet-console-dummy",
    "AWS_ACCESS_KEY_ID_gcvmfleet": "fleet-console-dummy",
    "AWS_SECRET_ACCESS_KEY_gcvmfleet": "fleet-console-dummy",
    "USER": "fleet-console",
}
PROFILE_OVERRIDES = ("experiment.run_id=fleet-console-profile", "experiment.jobs=1", "experiment.agents_per_replica=1")


@dataclass
class Profile:
    sha: str
    layout: str  # harbor_new | legacy
    groups: dict[str, list[str]] = field(default_factory=dict)
    cli_flags: list[str] = field(default_factory=list)
    print_config: bool = False
    steps: list[str] = field(default_factory=list)
    harbor_version: str | None = None
    agent_image: str | None = None
    missing: list[str] = field(default_factory=list)

    @property
    def compatible(self) -> bool:
        return self.layout == "harbor_new" and self.print_config and bool(self.steps)

    def supports_step(self, name: str) -> bool:
        return name in self.steps

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        return cls(**data)


def list_groups(configs: Path) -> dict[str, list[str]]:
    """Группы Hydra = подкаталоги configs (рекурсивно); члены — yaml по имени без расширения."""
    groups: dict[str, list[str]] = {}
    for d in sorted(p for p in configs.rglob("*") if p.is_dir()):
        members = sorted(f.stem for f in d.glob("*.yaml"))
        if members:
            groups[d.relative_to(configs).as_posix()] = members
    return groups


def harbor_version(worktree: Path) -> str | None:
    pyproject = worktree / "pyproject.toml"
    if not pyproject.exists():
        return None
    match = re.search(r'"harbor==([^"]+)"', pyproject.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def parse_help(text: str) -> list[str]:
    """Флаги из `fleet --help`: все уникальные токены вида --name / -n из колонки опций."""
    flags: list[str] = []
    for match in re.finditer(r"(?m)^\s*((?:-{1,2}[\w-]+(?:\s*/\s*-{1,2}[\w-]+)?(?:,\s*)?)+)", text):
        for flag in re.findall(r"-{1,2}[\w-]+", match.group(1)):
            if flag not in flags:
                flags.append(flag)
    return flags


def step_sections(resolved: dict[str, Any]) -> list[str]:
    """Ключи глубины 2 с `_target_`: system.materialize, mlsc.create_job и т. д."""
    steps: list[str] = []
    for top, section in resolved.items():
        if not isinstance(section, dict):
            continue
        for name, value in section.items():
            if isinstance(value, dict) and "_target_" in value:
                steps.append(f"{top}.{name}")
    return steps


async def build(runner: Runner, worktree: Path, sha: str) -> Profile:
    """Собрать профиль: читает файлы worktree и дважды зовёт fleet (--help, --print-config)."""
    configs = worktree / CONFIGS_REL
    profile = Profile(sha=sha, layout="harbor_new" if configs.is_dir() else "legacy")
    profile.harbor_version = harbor_version(worktree)
    if profile.layout == "legacy":
        profile.missing.append(f"нет каталога {CONFIGS_REL.as_posix()} — старая раскладка, пресеты несовместимы")
    else:
        profile.groups = list_groups(configs)

    help_result = await runner.run(fleet_argv(worktree, "--help"), cwd=worktree, timeout=120)
    if not help_result.ok:
        profile.missing.append(f"`fleet --help` не запускается: {_tail(help_result.stderr or help_result.stdout)}")
        return profile
    profile.cli_flags = parse_help(help_result.stdout)
    profile.print_config = "--print-config" in profile.cli_flags
    if not profile.print_config:
        profile.missing.append("в CLI нет флага --print-config — итоговый конфиг и превью недоступны")
        return profile
    if profile.layout == "legacy":
        return profile

    template = configs / TEMPLATE
    if not template.exists():
        profile.missing.append(f"нет {TEMPLATE} — секции-шаги не определены")
        return profile
    overrides: list[str] = []
    for group in REQUIRED_GROUPS:
        members = profile.groups.get(group)
        if not members:
            profile.missing.append(f"группа {group} пуста")
            return profile
        overrides += ["-o", f"{group}={members[0]}"]
    for override in PROFILE_OVERRIDES:
        overrides += ["-o", override]
    result = await runner.run(
        fleet_argv(worktree, str(template.relative_to(worktree)), "--print-config", *overrides),
        cwd=worktree,
        env=DUMMY_ENV,
        timeout=180,
    )
    if not result.ok:
        profile.missing.append(f"--print-config упал: {_tail(result.stderr or result.stdout)}")
        return profile
    try:
        resolved = yaml.safe_load(result.stdout) or {}
    except yaml.YAMLError as exc:
        profile.missing.append(f"--print-config вернул не YAML: {exc}")
        return profile
    profile.steps = step_sections(resolved)
    system = resolved.get("system") if isinstance(resolved, dict) else None
    if isinstance(system, dict):
        profile.agent_image = system.get("agent_image")
    return profile


def _tail(text: str, lines: int = 2) -> str:
    return " | ".join(text.strip().splitlines()[-lines:]) if text.strip() else "без вывода"
