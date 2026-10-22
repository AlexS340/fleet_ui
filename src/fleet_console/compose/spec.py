"""Спецификация бенч-прогона: exp.yaml в каталоге прогона, служебные overrides, команды fleet. Ничего не запускает.

exp.yaml — обычный эксперимент fleet (defaults и значения). Всё, что привязывает его к каталогу прогона
и к стадии (searchpath, run_id, output_dir, реестр, под MLSC, main.run), уходит в командную строку: так
стадии видят друг друга, артефакты не попадают в worktree, а команду можно повторить в терминале.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

import yaml

from fleet_console.fleetref.profile import CONFIGS_REL
from fleet_console.fleetref.sync import fleet_argv
from fleet_console.presets import DatasetRef

SLUG_RE = re.compile(r"[^a-z0-9]+")
LABEL_MAX = 63  # run_id — значение лейбла fleet/run-name
PREFIX_MAX = 45  # как default_workflow_prefix fleet: generate_name workflow + суффиксы fleet и Argo
PREFIX_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def workflow_prefix(benchmark: str) -> str:
    """`h-<benchmark>`, годный для generate_name Argo. fleet кладёт experiment.workflow_prefix в имя workflow как есть,
    а интерполяция `h-${harbor_dataset.benchmark}` пропускает подчёркивания из реестра — сервис чистит сам."""
    slug = SLUG_RE.sub("-", benchmark.lower()).strip("-") or "bench"
    return f"h-{slug}"[:PREFIX_MAX].rstrip("-")


def prefix_problem(prefix: str) -> str:
    """Почему префикс не годится для имени workflow; пусто — годится."""
    if not PREFIX_RE.match(prefix):
        return f"«{prefix}»: допустимы только строчные латинские буквы, цифры и дефис"
    if len(prefix) > PREFIX_MAX:
        return f"«{prefix}» длиннее {PREFIX_MAX} символов"
    return ""


def make_bench_id(name: str, now: datetime) -> str:
    """`<имя>-<время>`: годится как лейбл k8s, S3-префикс и имя каталога; fleet суффикс не добавит (run_id передаётся)."""
    stamp = now.strftime("%Y%m%d-%H%M%S")
    slug = SLUG_RE.sub("-", name.lower()).strip("-")[: LABEL_MAX - len(stamp) - 1].rstrip("-") or "bench"  # без «--» на стыке
    return f"{slug}-{stamp}"


@dataclass(frozen=True)
class SessionLink:
    """Бенч на уже поднятой сессии модели: подъём и гашение пропускаются, имя и реплики — из сессии."""

    id: str
    model: str
    recipe: str
    replicas: int
    version_id: int | None = None


@dataclass(frozen=True)
class BenchSpec:
    run_id: str
    worktree: Path
    run_dir: Path
    overlay: Path  # overlay/configs сервиса
    recipe: str
    agent: str
    dataset: str | DatasetRef
    jobs: int = 1
    agents_per_replica: int = 1
    task_limit: int | None = None
    profile: str = "production"
    inference: str = "litellm"
    model_name: str = ""  # пусто — served_model_name из рецепта
    harbor_job: dict[str, Any] = field(default_factory=dict)
    extra: tuple[str, ...] = ()  # overrides из «Дополнительно» и пресета
    pod: str = ""  # живой под MLSC; пусто — из mlsc/common.yaml ревизии
    session: SessionLink | None = None
    preset: str = ""
    sha: str = ""

    # --- пути -------------------------------------------------------------
    @property
    def exp(self) -> Path:
        return self.run_dir / "exp.yaml"

    @property
    def spec_file(self) -> Path:
        return self.run_dir / "spec.json"

    @property
    def resolved(self) -> Path:
        return self.run_dir / "resolved.yaml"

    @property
    def overrides_file(self) -> Path:
        return self.run_dir / "overrides.txt"

    @property
    def version_id_file(self) -> Path:
        return self.run_dir / "version_id"

    @property
    def registry_dir(self) -> Path:
        return self.run_dir / "registry"

    @property
    def out_dir(self) -> Path:
        return self.run_dir / "out"

    @property
    def logs_dir(self) -> Path:
        return self.run_dir / "logs"

    @property
    def dataset_label(self) -> str:
        return self.dataset if isinstance(self.dataset, str) else self.dataset.label

    def benchmark(self) -> str:
        """harbor_dataset.benchmark прогона: из пути или из файла группы в порядке поиска Hydra (каталог прогона,
        ревизия, overlay). Пусто — файла нет или в нём нет benchmark."""
        if isinstance(self.dataset, DatasetRef):
            return self.dataset.benchmark
        for directory in (self.run_dir, self.worktree / CONFIGS_REL, self.overlay):
            path = directory / "harbor" / "dataset" / f"{self.dataset}.yaml"
            if path.is_file():
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                except (OSError, yaml.YAMLError):
                    return ""
                bench = (data.get("harbor_dataset") or {}).get("benchmark") if isinstance(data, dict) else None
                return str(bench or "")
        return ""

    @property
    def effective_jobs(self) -> int:
        return self.session.replicas if self.session else self.jobs

    @property
    def effective_model(self) -> str:
        return self.session.model if self.session else self.model_name

    # --- exp.yaml -----------------------------------------------------------
    def exp_text(self) -> str:
        head = [f"# Прогон Fleet Console: {self.run_id}" + (f" · пресет {self.preset}" if self.preset else "")]
        if self.sha:
            head.append(f"# Ревизия fleet: {self.sha}")
        head += [
            "# Обычный эксперимент fleet: список defaults и значения. Служебные overrides (hydra.searchpath,",
            "# experiment.run_id, output_dir, реестр датасетов, под MLSC, main.run) сервис передаёт в командной",
            "# строке каждой стадии: они в overrides.txt рядом и в карточке прогона. Файл можно править —",
            "# правка действует только в этом прогоне и остаётся в его журнале.",
            "defaults:",
            "  - system: cluster            # стенд: кластер, S3, Polar, определения всех шагов",
            "  - mlsc/common@mlsc           # общие шаги MLSC: start_llm, check, stop",
            f"  - mlsc/model: {self.session.recipe if self.session else self.recipe}",
            "  - harbor/job: default        # скелет JobConfig",
            f"  - harbor/agent: {self.agent}",
        ]
        if isinstance(self.dataset, str):
            head.append(f"  - harbor/dataset: {self.dataset}")
        head += [
            "  - harbor/source: composed",
            f"  - execution: {self.profile}",
            "  - _self_",
            "",
            "output_dir: ???                # задаёт сервис: <каталог прогона>/out",
            "",
            "experiment:",
            "  run_id: ???                  # задаёт сервис: имя каталога прогона",
            "  model_name: ${mlsc.served_model_name}",
            f"  jobs: {self.jobs}" + ("                      # у сессии своё число реплик: сервис переопределяет" if self.session else ""),
            f"  agents_per_replica: {self.agents_per_replica}",
            (f"  workflow_prefix: {workflow_prefix(bench)}" + "   # h-<benchmark>, очищен сервисом: имя workflow Argo")
            if (bench := self.benchmark())
            else "  workflow_prefix: h-${harbor_dataset.benchmark}",
            f"  inference: {self.inference}",
            "",
            "execution:",
            f"  task_limit: {self.task_limit if self.task_limit else 'null'}            # null — все задачи датасета",
        ]
        body: dict[str, Any] = {}
        if self.model_name and not self.session:
            body["mlsc"] = {"served_model_name": self.model_name}
        harbor_job = dict(self.harbor_job)
        if isinstance(self.dataset, DatasetRef):
            harbor_job["datasets"] = [{"path": "${system.datasets_root}/" + self.dataset.path.strip("/"), "n_tasks": "${execution.task_limit}"}]
            body["harbor_dataset"] = {"benchmark": self.dataset.benchmark}
        if harbor_job:
            body["harbor_job"] = harbor_job
        text = "\n".join(head) + "\n"
        if body:
            text += "\n" + yaml.safe_dump(body, allow_unicode=True, sort_keys=False, width=200)
        text += "\nmain:\n  run: ???                     # задаёт сервис: шаги текущей стадии\n"
        return text

    def write(self, force: bool = False) -> Path:
        """Каталог прогона: exp.yaml (если нет или force — ручные правки не затираются), overrides.txt, fleet.sha."""
        for d in (self.run_dir, self.logs_dir, self.out_dir):
            d.mkdir(parents=True, exist_ok=True)
        if force or not self.exp.exists():
            self.exp.write_text(self.exp_text(), encoding="utf-8")
        self.overrides_file.write_text(
            "# Служебные overrides сервиса, одинаковые для всех стадий; плюс main.run=[…] на стадию.\n"
            + "\n".join(self.service_overrides())
            + "\n",
            encoding="utf-8",
        )
        if self.sha:
            (self.run_dir / "fleet.sha").write_text(self.sha + "\n", encoding="utf-8")
        self.spec_file.write_text(self.to_json(), encoding="utf-8")
        return self.exp

    # --- сохранение -----------------------------------------------------------
    def to_json(self) -> str:
        data = asdict(self)
        for key in ("worktree", "run_dir", "overlay"):
            data[key] = str(data[key])
        data["dataset"] = self.dataset if isinstance(self.dataset, str) else self.dataset.model_dump()
        data["extra"] = list(self.extra)
        return json.dumps(data, ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, text: str, *, pod: str = "", session: SessionLink | None = None) -> "BenchSpec":
        """Спецификация из spec.json; под MLSC и сессия — живые, подставляются заново."""
        data = json.loads(text)
        data["worktree"], data["run_dir"], data["overlay"] = Path(data["worktree"]), Path(data["run_dir"]), Path(data["overlay"])
        if isinstance(data["dataset"], dict):
            data["dataset"] = DatasetRef.model_validate(data["dataset"])
        data["extra"] = tuple(data.get("extra") or ())
        data["pod"] = pod or data.get("pod", "")
        stored = data.get("session")
        data["session"] = session or (SessionLink(**stored) if stored else None)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    # --- команды ------------------------------------------------------------
    def service_overrides(self) -> list[str]:
        values = [
            f"hydra.searchpath=[file://{self.worktree / CONFIGS_REL},file://{self.overlay}]",
            f"experiment.run_id={self.run_id}",
            f"output_dir={self.out_dir}",
            f"mlsc.version_id_file={self.version_id_file}",
            f"system.materialize.dataset_registry.directory={self.registry_dir}",
        ]
        if self.pod:
            values.append(f"mlsc.pod_name={self.pod}")
        values.append(f"mlsc.create_job.job.application_name='fleet-console {self.run_id}'")
        if self.session:
            values += [f"mlsc.served_model_name={self.session.model}", f"experiment.jobs={self.session.replicas}"]
        values.extend(self.extra)
        return values

    def overrides(self, steps: list[str]) -> list[str]:
        return [*self.service_overrides(), "main.run=[" + ",".join(steps) + "]"]

    def argv(self, steps: list[str], print_config: bool = False) -> list[str]:
        args = [str(self.exp)] + (["--print-config"] if print_config else [])
        for value in self.overrides(steps):
            args += ["-o", value]
        return fleet_argv(self.worktree, *args)

    def read_version_id(self) -> int | None:
        try:
            return int(self.version_id_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
