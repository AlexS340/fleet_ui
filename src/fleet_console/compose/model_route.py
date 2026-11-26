"""Проверка маршрута бенча по --print-config и свежему списку моделей шлюза."""

from pathlib import Path
from urllib.parse import urlsplit

import yaml

from fleet_console.compose.preview import Preview
from fleet_console.compose.spec import BenchSpec
from fleet_console.connect.gateway import cluster_models, parse_target
from fleet_console.observe.nginx import model_replicas
from fleet_console.runner import Runner
from fleet_console.settings import Settings


def _agents_problem(job: object, field: str, expected: str) -> str:
    agents = job.get("agents") if isinstance(job, dict) else None
    if not isinstance(agents, list) or not agents:
        return f"В {field}.agents нет агентов"
    for index, agent in enumerate(agents):
        model = agent.get("model_name") if isinstance(agent, dict) else None
        if not isinstance(model, str) or model.removeprefix("openai/") != expected:
            return f"{field}.agents[{index}].model_name={model!r}"
    return ""


def _source_problem(source: object, field: str, worktree: Path, expected: str) -> str:
    if not isinstance(source, dict):
        return f"В {field} нет JobConfig"
    config = source.get("config") or {}
    # При merge=true явный список агентов заменяет список из базового JobConfig.
    if isinstance(config, dict) and "agents" not in config and isinstance(source.get("path"), str):
        path = worktree / source["path"]
        try:
            config = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            return f"Не удалось прочитать список агентов из {path}"
        field = str(path)
    return _agents_problem(config, field, expected)


async def model_route_problem(spec: BenchSpec, resolved: dict, preview: Preview, runner: Runner,
                              settings: Settings, *, session_ready: bool) -> str:
    experiment = resolved.get("experiment") or {}
    model = experiment.get("model_name")
    served = (resolved.get("mlsc") or {}).get("served_model_name")
    expected = spec.effective_model or served
    names = f"experiment.model_name={model!r}, mlsc.served_model_name={served!r}"
    if spec.session:
        names += f", модель сессии={spec.session.model!r}"
    if not isinstance(model, str) or not model.strip() or model != served or model != expected:
        return f"{names}. Ожидалось {expected!r}"

    materialize = (resolved.get("system") or {}).get("materialize") or {}
    source = materialize.get("job_source")
    if source is None:
        problem = _agents_problem(resolved.get("harbor_job") or {}, "harbor_job", model)
    else:
        problem = _source_problem(source, "system.materialize.job_source.config", spec.worktree, model)
    if problem:
        return f"{problem}. {names}"
    for index, job in enumerate(materialize.get("additional_jobs") or []):
        field = f"system.materialize.additional_jobs[{index}].job_source.config"
        source = job.get("job_source") if isinstance(job, dict) else None
        if problem := _source_problem(source, field, spec.worktree, model):
            return f"{problem}. {names}"

    if preview.inference not in {"nginx", "litellm"}:
        return f"Неизвестный маршрут experiment.inference={preview.inference!r}"
    if preview.inference == "nginx":
        namespace, service, port = parse_target(settings.cluster.nginx_gateway)
        dns = f"{service}.{namespace}"
        try:
            target = urlsplit(preview.gateway)
            host = (target.hostname or "").rstrip(".")
            target_port = target.port or (443 if target.scheme == "https" else 80)
        except ValueError:
            return "Некорректный base_url шлюза Polar"
        # В штатном кластере тот же nginx доступен через headless-сервис в argo.
        hosts = {dns}
        if (namespace, service) == ("vllm-proxy", "nginx-vllm-proxy-test"):
            hosts.add("vllm-proxy.argo")
        host = host.partition(".svc.")[0].removesuffix(".svc")
        if namespace == settings.namespace:
            hosts.add(service)
        if target.scheme not in {"http", "https"} or host not in hosts or target_port != port:
            return f"Для nginx ожидается шлюз {dns}:{port}, в Polar base_url={preview.gateway!r}"

    if not spec.session or not session_ready:
        return ""
    if not preview.gateway:
        return "В итоговом конфиге отсутствует base_url шлюза Polar"
    try:
        if preview.inference == "nginx":
            cluster = settings.cluster
            replicas = await model_replicas(runner, cluster.mlsc_namespace, cluster.nginx_configmap, cluster.nginx_key)
            if model not in replicas:
                return f"В upstream nginx отсутствует точный ID {model!r}"
            if replicas[model] == 0:
                return f"В upstream nginx для {model!r} нет реплик"
        else:
            models = await cluster_models(runner, settings.namespace, settings.cluster.submitter_pod, preview.gateway)
            if model not in {m.id for m in models}:
                return f"В ответе LiteLLM /v1/models отсутствует точный ID {model!r}"
    except (OSError, RuntimeError, ValueError) as exc:
        return f"Не удалось прочитать модели шлюза {preview.inference}: {exc}"
    return ""
