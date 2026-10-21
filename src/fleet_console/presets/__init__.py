"""Пресеты бенчей: YAML в presets/ репозитория сервиса. Пресет ссылается на группы ревизии и overlay по имени.

Пресет задаёт только то, что отличается от дефолтов fleet; форма показывает его поля и даёт поправить любое.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
import yaml

from fleet_console.settings import PRESETS_DIR


class FleetRef(BaseModel):
    ref: str = "master"  # ветка, тег или SHA; при запуске разрешается в SHA


class DatasetRef(BaseModel):
    """Датасет не из группы harbor/dataset, а прямо: путь относительно FLEET_DATASETS_ROOT и имя бенчмарка."""

    path: str
    benchmark: str
    mapping: str | None = None  # имя файла реестра datasets/ (ревизии или overlay); None — по tasks_root

    @property
    def label(self) -> str:
        return self.path


class Resources(BaseModel):
    jobs: int = Field(1, ge=1)  # реплики модели
    agents_per_replica: int = Field(1, ge=1)


class Execution(BaseModel):
    profile: str = "production"  # группа execution/
    task_limit: int | None = Field(None, ge=1)  # null — все задачи датасета


class Preset(BaseModel):
    name: str
    description: str = ""
    fleet: FleetRef = Field(default_factory=FleetRef)
    model: str  # mlsc/model
    model_name: str = ""  # served_model_name поверх рецепта (суффикс против коллизий)
    agent: str  # harbor/agent (ревизии или overlay)
    dataset: str | DatasetRef  # harbor/dataset по имени или путь + benchmark
    resources: Resources = Field(default_factory=Resources)
    execution: Execution = Field(default_factory=Execution)
    inference: Literal["litellm", "nginx"] = "litellm"
    harbor_job: dict[str, Any] = Field(default_factory=dict)  # поверх harbor_job: таймауты и т. п.
    overrides: list[str] = Field(default_factory=list)  # сырые overrides fleet, как в «Дополнительно»
    path: str = ""  # откуда загружен

    @property
    def dataset_label(self) -> str:
        return self.dataset if isinstance(self.dataset, str) else self.dataset.label


def load_presets(directory: Path = PRESETS_DIR) -> tuple[list[Preset], list[str]]:
    """Все *.yaml каталога; битые файлы не роняют остальные — их ошибки возвращаются списком."""
    presets: list[Preset] = []
    errors: list[str] = []
    if not directory.is_dir():
        return presets, errors
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise ValueError("ожидался словарь")
            data.setdefault("name", path.stem)
            presets.append(Preset.model_validate({**data, "path": str(path)}))
        except (OSError, yaml.YAMLError, ValueError, ValidationError) as exc:
            errors.append(f"{path.name}: {str(exc).splitlines()[0]}")
    return presets, errors


def compatibility(preset: Preset, groups: dict[str, list[str]], steps: list[str], required_steps: list[str]) -> list[str]:
    """Почему пресет не подходит к ревизии: нет группы, члена или шага. Пустой список — совместим.

    `groups` — группы ревизии, уже объединённые с overlay.
    """
    problems: list[str] = []
    for group, member in (("mlsc/model", preset.model), ("harbor/agent", preset.agent), ("execution", preset.execution.profile)):
        if member not in groups.get(group, []):
            problems.append(f"в ревизии нет {group}={member}")
    if isinstance(preset.dataset, str) and preset.dataset not in groups.get("harbor/dataset", []):
        problems.append(f"в ревизии нет harbor/dataset={preset.dataset}")
    if isinstance(preset.dataset, DatasetRef) and preset.dataset.mapping and preset.dataset.mapping not in groups.get("datasets", []):
        problems.append(f"в реестре datasets нет {preset.dataset.mapping}")
    missing = [s for s in required_steps if steps and s not in steps]
    if missing:
        problems.append("в ревизии нет шагов: " + ", ".join(missing))
    return problems


def matches_ref(preset: Preset, ref: str, sha: str) -> bool:
    """Пресет написан под ревизию: ту же ветку/тег, SHA или шаблон (`release/*`)."""
    want = preset.fleet.ref
    return want in {ref, sha} or sha.startswith(want) or fnmatch(ref, want)
