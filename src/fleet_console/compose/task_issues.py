"""Применение каталога несовместимостей к выбранным Harbor парам агент × задача."""

import asyncio
import json
from collections import defaultdict
from fnmatch import fnmatchcase
from pathlib import Path

from fleet_console.compose.preflight import Check, Issue, load_issues
from fleet_console.compose.spec import BenchSpec
from fleet_console.compose.verifier_image import script_present
from fleet_console.fleetref.profile import DUMMY_ENV, harbor_version
from fleet_console.runner import Runner


def match_tasks(issues: list[Issue], tasks: list[dict], runner_image: str) -> list[Check]:
    checks = []
    inspected = {}
    for issue in issues:
        groups = defaultdict(set)
        for task in tasks:
            value = task.get(issue.feature)
            if not value or (issue.job_agent and not fnmatchcase(task["agent"], issue.job_agent)):
                continue
            name = Path(task["path"]).name
            level = issue.level
            if issue.buildx:
                if any(fnmatchcase(runner_image, p) for p in issue.buildx["present"]):
                    continue
                if any(fnmatchcase(runner_image, p) for p in issue.buildx["missing"]):
                    evidence = "Buildx отсутствует"
                else:
                    evidence, level = "наличие Buildx неизвестно", "warn"
                groups[level, f"{runner_image or '?'} ({evidence}, {', '.join(value)})"].add(name)
            elif issue.feature == "verifier_images":
                for image in value:
                    present = None
                    if image not in task.get("unverified_images", []):
                        if image not in inspected:
                            inspected[image] = script_present(image)
                        present = inspected[image]
                    evidence = {True: "скрипт найден", False: "скрипт отсутствует", None: "содержимое не подтверждено"}[present]
                    groups["fail" if present is False else level, f"{image} ({evidence})"].add(name)
            else:
                detail = "Сервисы: " + ", ".join(value) + "." if isinstance(value, list) else ""
                groups[level, detail].add(name)
        for (level, evidence), names in groups.items():
            detail = " ".join(filter(None, (issue.message, evidence, "Задачи: " + ", ".join(sorted(names)))))
            checks.append(Check(f"issue:{issue.id}", "Известная несовместимость", level, detail))
    return checks


async def task_issue_checks(runner: Runner, spec: BenchSpec, resolved: dict) -> list[Check]:
    version = harbor_version(spec.worktree)
    issues = [i for i in load_issues() if i.feature and i.matches(spec.agent, spec.recipe, spec.dataset_label, version)]
    if not issues:
        return []
    system = resolved.get("system") or {}
    materialize = system.get("materialize") or {}
    probe = Path(__file__).with_name("task_compatibility.py")
    try:
        result = await runner.run([str(spec.worktree / ".venv/bin/python"), "-I", "-B", str(probe)],
                                  cwd=spec.worktree, env=DUMMY_ENV, timeout=300, stdin=json.dumps(materialize))
        if result.ok and isinstance(tasks := json.loads(result.stdout), list):
            builder = (system.get("manifest_runner") or {}).get("workflow_builder")
            if builder is None:
                builder = system.get("workflow_builder") or system
            image = builder.get("agent_image")
            return await asyncio.to_thread(match_tasks, issues, tasks, image or "")
    except (OSError, TimeoutError, ValueError):
        pass
    return [Check("task_compatibility", "Совместимость задач и раннера", "warn",
                  "Не удалось проверить признаки выбранных задач средствами этой ревизии.")]
