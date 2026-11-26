"""Выполняется Python выбранного worktree после разрешения списка задач Harbor."""

import sys
from pathlib import Path

import yaml

if __package__:
    from .task_features import TaskFeatures
else:
    # -I исключает cwd и каталог скрипта. Добавляем только каталог Console.
    sys.path.append(str(Path(__file__).resolve().parent))
    from task_features import TaskFeatures


def compose_conflicts(context: Path, overlays: list[dict]) -> list[str]:
    """Сервисы с extra_hosts, которым Harbor назначит namespace sidecar либо уже задан service:…."""
    path = context / "docker-compose.yaml"
    documents = [yaml.safe_load(path.read_text())] if path.is_file() else []
    services = {}
    for document in [*documents, *overlays]:
        if not isinstance(document, dict) or {"include", "extends"} & document.keys():
            raise ValueError("не удалось разрешить Compose")
        for name, service in (document.get("services") or {}).items():
            service = service or {}
            if not isinstance(service, dict) or "extends" in service or service.get("profiles") or "$" in str(service.get("network_mode", "")):
                raise ValueError("не удалось разрешить сеть Compose")
            merged = services.setdefault(name, {})
            if service.get("extra_hosts"):
                merged["extra_hosts"] = True
            for key in ("network_mode", "networks"):
                if key in service:
                    merged[key] = service[key]
    return sorted(name for name, service in services.items()
                  if service.get("extra_hosts") and (
                      str(service.get("network_mode", "")).startswith("service:")
                      or ("networks" not in service and "network_mode" not in service
                          and name != "harbor-docker-egress-control-sidecar")))


def inspect_network(item, context, env, policies, documents, extra):
    restricted = {p.network_mode.value for p in policies if p and p.network_mode != "public"}
    if not restricted or env.os != "linux":
        return
    item["egress"].update(restricted)
    if extra:
        # Произвольный overlay загружается по пути внутри пода.
        item["unknown_compose"] = True
        return
    try:
        item["compose_conflict"].update(compose_conflicts(context, documents))
    except (ValueError, OSError, yaml.YAMLError):
        item["unknown_compose"] = True


def job_features(materialize: dict, job, paths: list[Path]) -> list[dict]:
    from fleet.harbor import HarborPackageMirrors, HarborPolarInference
    from harbor.models.task.task import Task
    from harbor.models.task.verifier_mode import (
        resolve_effective_verifier_env_config,
        resolve_step_verifier_mode,
        resolve_task_verifier_mode,
    )
    from harbor.trial.network_policy import resolve_trial_network_plan

    # Значения по умолчанию и содержимое генерируемых Fleet overlays берём из выбранного SHA.
    mirrors = HarborPackageMirrors.model_validate(materialize["package_mirrors"])
    mirror_doc = yaml.safe_load(mirrors.files()["docker-compose-mirrors.yaml"])
    polar = HarborPolarInference.model_validate(materialize.get("polar") or {})
    polar_doc = yaml.safe_load(polar.files()[polar.compose_filename])
    selected = [(path, Task(path, disable_verification=job.verifier.disable).config) for path in paths]
    result = []
    for agent in job.agents:
        name = agent.name or ""
        # Fleet материализует по одному агенту. Polar может добавить host-gateway и allowlist.
        effective = polar.apply_to_job({"agents": [agent.model_dump()], "environment": job.environment.model_dump()},
                                       runtime_dir=mirrors.runtime_dir)
        trial_env = type(job.environment).model_validate(effective["environment"])
        trial_agent = type(agent).model_validate(effective["agents"][0])
        overlays = [mirror_doc, polar_doc] if polar.uses_in_container_llm(name) else [mirror_doc]
        for path, task in selected:
            facts = TaskFeatures.from_config(task.model_dump(mode="json", exclude_none=True))
            item = {"path": str(path), "agent": name, "mcp": facts.has_mcp}
            result.append(item)
            if trial_env.type not in (None, "docker") or trial_env.import_path:
                continue
            item.update(egress=set(), compose_conflict=set())
            images = set()
            unverified = set()

            for step in task.steps or [None]:
                mode = resolve_step_verifier_mode(task, step) if step else resolve_task_verifier_mode(task)
                verifier_env = resolve_effective_verifier_env_config(task, step)
                plan = resolve_trial_network_plan(task, trial_agent, trial_env, step, verifier_mode=mode)
                main = [plan.agent_env_baseline, plan.agent_phase]
                if verifier_env is None:
                    main.append(plan.verifier_phase)
                inspect_network(item, path / "environment", task.environment, main, overlays, job.environment.extra_docker_compose)
                if verifier_env is not None and not job.verifier.disable:
                    context = path / "tests"
                    if step and (step_dir := path / "steps" / step.name / "tests").is_dir():
                        context = step_dir
                    inspect_network(item, context, verifier_env, [plan.verifier_env_baseline, plan.verifier_phase], [], [])
                    if verifier_env.docker_image and verifier_env.os == "linux":
                        images.add(verifier_env.docker_image)
                        # Compose или пользовательский mount может заменить /tests внутри контейнера.
                        if (context / "docker-compose.yaml").exists() or trial_env.mounts:
                            unverified.add(verifier_env.docker_image)
            item.update(egress=sorted(item["egress"]), compose_conflict=sorted(item["compose_conflict"]), verifier_images=sorted(images),
                        unverified_images=sorted(unverified))
    return result


async def selected_features(materialize: dict) -> list[dict]:
    from fleet.harbor import HarborJobConfigSource

    result = []
    for entry in [materialize, *(materialize.get("additional_jobs") or [])]:
        job = HarborJobConfigSource.model_validate(entry["job_source"]).load()
        paths = [task.path for task in job.tasks]
        for dataset in job.datasets:
            if not dataset.is_local():
                raise ValueError("датасет без локального пути")
            paths.extend(task.path for task in await dataset.get_task_configs(disable_verification=job.verifier.disable))
        result.extend(job_features(materialize, job, list(dict.fromkeys(paths))))
    return result


if __name__ == "__main__":
    import asyncio
    import json

    print(json.dumps(asyncio.run(selected_features(json.load(sys.stdin)))))
