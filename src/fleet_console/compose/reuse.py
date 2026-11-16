"""Настройки запуска, переносимые из истории в новую форму."""

from pathlib import Path
import re

import yaml

from fleet_console.compose.sources import defaults_of
from fleet_console.compose.spec import BenchSpec
from fleet_console.presets import Execution, FleetRef, Preset, Resources


def config_path(name: str) -> Path:
    path = Path(name)
    if not re.fullmatch(r"(?:[\w.-]+/)*[\w.-]+\.yaml", name) or ".." in path.parts:
        raise ValueError("Недопустимый путь к YAML прогона")
    return path


def preset_from_run(spec: BenchSpec, directory: Path, *, name: str, sha: str) -> Preset:
    """Собираем из исходных настроек, без resolved.yaml, ID, логов и адреса результатов."""
    exp = (directory / "exp.yaml").read_text(encoding="utf-8")
    config_files = {"exp.yaml": exp} if yaml.safe_load(exp) != yaml.safe_load(spec.exp_text()) else {}
    for group, member in defaults_of(exp):
        if not group:
            continue  # на верхнем уровне хранятся служебные YAML, их не переносим
        rel = f"{group}/{member}.yaml"
        path = directory / config_path(rel)
        if path.is_file():
            config_files[rel] = path.read_text(encoding="utf-8")
    return Preset(
        name=name, fleet=FleetRef(ref=sha), model=spec.recipe, model_name=spec.model_name, agent=spec.agent,
        dataset=spec.dataset, inference=spec.inference, resources=Resources(jobs=spec.jobs, agents_per_replica=spec.agents_per_replica),
        execution=Execution(profile=spec.profile, task_limit=spec.task_limit), harbor_job=spec.harbor_job,
        overrides=list(spec.extra), config_files=config_files, base_preset=spec.preset,
    )
