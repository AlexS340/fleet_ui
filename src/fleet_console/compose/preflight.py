"""Preflight: список проверок до выделения GPU. «Запустить» активна только без ✗; ⚠ принимаются явно."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

from fleet_console.compose.registry import Mapping, find_mapping
from fleet_console.compose.spec import prefix_problem
from fleet_console.connect.kube import KubeStatus
from fleet_console.fleetref.profile import Profile
from fleet_console.settings import KNOWN_ISSUES, SECRET_NAMES

# Шаги fleet, без которых стадии сервиса не собрать.
STEPS_COMMON = [
    "system.materialize",
    "system.topology_saver",
    "system.manifest_runner",
    "mlsc.create_agent_limit",
    "system.runtime_upload",
    "system.task_artifacts_upload",
    "system.pod_upload",
    "system.submitter_launch",
    "system.measure_run",
]
STEPS_MODEL = ["mlsc.create_job", "mlsc.start_llm", "mlsc.check_llm_works", "mlsc.stop_llm"]
STEPS_OPTIONAL = ["system.requeue_failed"]


def required_steps(own_model: bool) -> list[str]:
    return STEPS_COMMON + (STEPS_MODEL if own_model else [])


@dataclass(frozen=True)
class Check:
    key: str
    title: str
    level: str  # ok | warn | fail
    detail: str = ""
    action: dict[str, Any] | None = None  # что UI может предложить: {"kind": "fetch_dataset", "mapping": …, "path": …}

    @property
    def ok(self) -> bool:
        return self.level == "ok"


def summary(checks: list[Check]) -> tuple[int, int]:
    return sum(1 for c in checks if c.level == "fail"), sum(1 for c in checks if c.level == "warn")


@dataclass(frozen=True)
class Issue:
    id: str
    message: str
    level: str = "warn"
    agent: str | None = None
    recipe: str | None = None
    dataset: str | None = None
    harbor: str | None = None

    def matches(self, agent: str, recipe: str, dataset: str, harbor: str | None) -> bool:
        pairs = ((self.agent, agent), (self.recipe, recipe), (self.dataset, dataset), (self.harbor, harbor or ""))
        return all(pattern is None or fnmatch(value, pattern) for pattern, value in pairs)


def load_issues(path: Path = KNOWN_ISSUES) -> list[Issue]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError):
        return []
    issues = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and item.get("id") and item.get("message"):
            issues.append(
                Issue(
                    str(item["id"]), str(item["message"]), str(item.get("level", "warn")),
                    item.get("agent"), item.get("recipe"), item.get("dataset"), item.get("harbor"),
                )
            )
    return issues


def run_checks(
    *,
    secrets: dict[str, str],
    kube: KubeStatus,
    profile: Profile,
    own_model: bool,
    resolved: dict[str, Any] | None,
    resolve_error: str,
    datasets_root: Path | None,
    mappings: list[Mapping],
    registry_errors: list[str],
    foreign_models: set[str],
    model_name: str,
    agent: str,
    recipe: str,
    dataset: str,
    issues: list[Issue],
    session_status: str | None = None,
    worktree_clean: bool = True,
    overlay_drift: list[str] | None = None,
) -> list[Check]:
    checks: list[Check] = []
    missing = [n for n in SECRET_NAMES if not secrets.get(n)]
    checks.append(
        Check(
            "secrets", "MLSC_TOKEN и S3-креды", "fail" if missing else "ok",
            "нет: " + ", ".join(missing) + " (окружение сервиса, secrets.env или профиль gcvmfleet в ~/.aws/credentials)" if missing else "есть",
        )
    )
    if not kube.allowed:
        checks.append(Check("kube", "Kube context", "fail", kube.error or "context не разрешён"))
    elif not kube.ready:
        checks.append(Check("kube", "Kube context", "fail", kube.error or "API-сервер не отвечает (ssh к бастиону?)"))
    elif kube.missing_permissions:
        checks.append(Check("kube", "Kube context", "fail", "нет прав: " + ", ".join(kube.missing_permissions)))
    else:
        checks.append(Check("kube", "Kube context", "ok", f"{kube.context}, права проверены"))

    steps_missing = [s for s in required_steps(own_model) if s not in profile.steps]
    if not profile.compatible:
        checks.append(Check("revision", "Ревизия fleet", "fail", "; ".join(profile.missing) or "несовместима"))
    elif steps_missing:
        checks.append(Check("revision", "Ревизия fleet", "fail", "нет шагов: " + ", ".join(steps_missing)))
    else:
        optional = [s for s in STEPS_OPTIONAL if s not in profile.steps]
        checks.append(
            Check("revision", "Ревизия fleet", "warn" if optional else "ok", "нет " + ", ".join(optional) + " — requeue не будет" if optional else f"все шаги есть, Harbor {profile.harbor_version or '?'}")
        )
    checks.append(Check("worktree", "Worktree чистый, uv sync прошёл", "ok" if worktree_clean else "fail", "" if worktree_clean else "git status не пуст"))

    if resolved is None:
        checks.append(Check("config", "Сборка конфига (--print-config)", "fail", resolve_error or "не запускался"))
    elif problem := prefix_problem(str((resolved.get("experiment") or {}).get("workflow_prefix") or "h-bench")):
        checks.append(Check("config", "Сборка конфига (--print-config)", "fail", f"experiment.workflow_prefix {problem}: workflow не создастся"))
    else:
        checks.append(Check("config", "Сборка конфига (--print-config)", "ok", "resolved.yaml записан"))

    if registry_errors:
        checks.append(Check("registry", "Реестр датасетов", "fail", "; ".join(registry_errors)))
    else:
        checks.append(Check("registry", "Реестр датасетов", "ok", f"маппингов {len(mappings)}"))

    if datasets_root is None:
        checks.append(Check("dataset", "Датасет под FLEET_DATASETS_ROOT", "fail", "FLEET_DATASETS_ROOT не задан: настройки → «Корень датасетов»"))
    elif resolved is not None:
        problems = []
        found = []
        action = None
        if not datasets_root.is_dir():  # корень создаст `aws s3 sync` — предложение скачать остаётся
            problems.append(f"корня {datasets_root} нет (создастся при скачивании)")
        for d in (resolved.get("harbor_job") or {}).get("datasets") or []:
            path = Path(str(d.get("path", ""))) if isinstance(d, dict) else None
            if path is None:
                continue
            m = find_mapping(mappings, datasets_root, path)
            if not path.is_dir() or not any(path.iterdir()):
                what = "нет каталога" if not path.is_dir() else "пустой каталог"
                problems.append(f"{what} {path}" + (f" — можно скачать из S3 по реестру {m.name}" if m else "; маппинга в реестре для него нет"))
                if m is not None and action is None:
                    action = {"kind": "fetch_dataset", "mapping": m.name, "path": str(path), "task_names": list(d.get("task_names") or [])}
                continue
            if m is None:
                problems.append(f"для {path} нет маппинга в реестре (tasks_root)")
            else:
                found.append(f"{path.name} → {m.name} ({m.origin})")
        level = "fail" if problems else ("ok" if found else "warn")
        checks.append(Check("dataset", "Датасет под FLEET_DATASETS_ROOT", level, "; ".join(problems or found) or "в конфиге нет harbor_job.datasets", action))
    elif not datasets_root.is_dir():
        checks.append(Check("dataset", "Датасет под FLEET_DATASETS_ROOT", "fail", f"каталога {datasets_root} нет"))
    else:
        checks.append(Check("dataset", "Датасет под FLEET_DATASETS_ROOT", "warn", "проверю после сборки конфига"))

    if overlay_drift:
        checks.append(Check("overlay", "Правки overlay против ревизии", "warn", "; ".join(overlay_drift) + " — сравните в полном конфиге"))

    checks.append(Check("materialize", "Материализация (mode=generate)", "ok", "первая стадия прогона, до GPU: битый датасет остановит прогон бесплатно"))

    if own_model:
        if model_name and model_name in foreign_models:
            checks.append(Check("model_name", "served_model_name свободен", "fail", f"{model_name} уже поднят чужим прогоном — задайте суффикс"))
        else:
            checks.append(Check("model_name", "served_model_name свободен", "ok", model_name or "из рецепта"))
    else:
        ok = session_status == "ready"
        checks.append(Check("session", "Сессия модели готова", "ok" if ok else "fail", session_status or "сессия не найдена"))

    for issue in issues:
        if issue.matches(agent, recipe, dataset, profile.harbor_version):
            checks.append(Check(f"issue:{issue.id}", "Известная несовместимость", issue.level if issue.level in {"warn", "fail"} else "warn", issue.message))
    checks.append(Check("quota", "Свободная квота GPU", "warn", "MLSC API квоту не отдаёт; смотрите занятые GPU в шапке"))
    return checks
