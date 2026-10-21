"""Сборка команд fleet для сессии модели: конфиг в каталоге сессии плюс overrides. Ничего не запускает.

Конфиг — копия `overlay/configs/console_model.yaml`; группы ревизии подключаются через
`hydra.searchpath`. Harbor-группы нужны как заглушки: без них `system: cluster` не резолвится
(проверено 2026-10-21), на шаги `mlsc.*` они не влияют.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import shutil
from typing import Any

import yaml

from fleet_console.fleetref.profile import CONFIGS_REL
from fleet_console.fleetref.sync import fleet_argv

OVERLAY_TEMPLATE = Path(__file__).resolve().parents[3] / "overlay" / "configs" / "console_model.yaml"
CONFIG_NAME = "console_model.yaml"
STEPS = {
    "create_job": "mlsc.create_job",
    "start_llm": "mlsc.start_llm",
    "check": "mlsc.check_llm_works",
    "stop": "mlsc.stop_llm",
}
GPU_RE = re.compile(r"(\d+)gpu")
SLUG_RE = re.compile(r"[^a-z0-9]+")


def make_run_id(model_name: str, now: datetime) -> str:
    """`console-<модель>-<время>`: годится как значение лейбла k8s и как имя каталога."""
    slug = SLUG_RE.sub("-", model_name.lower()).strip("-")[:30] or "model"
    return f"console-{slug}-{now.strftime('%Y%m%d-%H%M%S')}"


def gpu_in(instance_type: str | None) -> int | None:
    match = GPU_RE.search(instance_type or "")
    return int(match.group(1)) if match else None


def recipe_summary(worktree: Path, recipe: str) -> dict[str, Any]:
    """served_model_name, тип инстанса и образ из рецепта mlsc/model — без Hydra, только для формы."""
    path = worktree / CONFIGS_REL / "mlsc" / "model" / f"{recipe}.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    found: dict[str, Any] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("served_model_name", "instance_type_name", "image") and isinstance(value, str) and key not in found:
                    found[key] = value
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    found["gpu_per_instance"] = gpu_in(found.get("instance_type_name"))
    return found


def write_config(run_dir: Path) -> Path:
    """Каталог сессии с копией конфига; существующий конфиг не трогаем (его могли править руками)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    target = run_dir / CONFIG_NAME
    if not target.exists():
        shutil.copyfile(OVERLAY_TEMPLATE, target)
    return target


@dataclass(frozen=True)
class SessionSpec:
    run_id: str
    worktree: Path
    run_dir: Path
    recipe: str
    replicas: int
    inference: str
    stub_agent: str
    stub_dataset: str
    pod: str
    model_name: str = ""  # пусто — served_model_name из рецепта
    extra: tuple[str, ...] = ()  # overrides из «Дополнительно»

    @property
    def config(self) -> Path:
        return self.run_dir / CONFIG_NAME

    @property
    def version_id_file(self) -> Path:
        return self.run_dir / "version_id"

    @property
    def resolved(self) -> Path:
        return self.run_dir / "resolved.yaml"

    def overrides(self, step: str, jobs: int | None = None) -> list[str]:
        values = [
            f"hydra.searchpath=[file://{self.worktree / CONFIGS_REL}]",
            f"mlsc/model={self.recipe}",
            f"harbor/agent={self.stub_agent}",
            f"harbor/dataset={self.stub_dataset}",
            f"experiment.run_id={self.run_id}",
            f"experiment.jobs={self.replicas if jobs is None else jobs}",
            f"experiment.inference={self.inference}",
            f"mlsc.version_id_file={self.version_id_file}",
            f"mlsc.pod_name={self.pod}",
            f"mlsc.create_job.job.application_name='fleet-console {self.run_id}'",
            f"output_dir={self.run_dir / 'out'}",
        ]
        if self.model_name:
            values.append(f"mlsc.served_model_name={self.model_name}")
        values.extend(self.extra)
        values.append(f"main.run=[{STEPS[step]}]")
        return values

    def argv(self, step: str, jobs: int | None = None, print_config: bool = False) -> list[str]:
        args = [str(self.config)] + (["--print-config"] if print_config else [])
        for value in self.overrides(step, jobs):
            args += ["-o", value]
        return fleet_argv(self.worktree, *args)

    def read_version_id(self) -> int | None:
        try:
            return int(self.version_id_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
