"""Превью «что и где»: карточка из resolved.yaml (`fleet --print-config`) и локальной материализации."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
from typing import Any

GPU_RE = re.compile(r"(\d+)gpu|gpu(\d+)")


def gpu_in(instance_type: str | None) -> int | None:
    m = GPU_RE.search(instance_type or "")
    return int(m.group(1) or m.group(2)) if m else None


def _get(data: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if isinstance(data, dict) and key in data:
            data = data[key]
        else:
            return default
    return data


@dataclass
class Preview:
    run_id: str = ""
    revision: str = ""  # ветка@sha, влита ли
    cluster: str = ""  # context, namespace
    namespace: str = ""
    model: str = ""
    model_image: str = ""
    instance_type: str = ""
    queue: str = ""
    replicas: int = 0
    gpu_per_instance: int | None = None
    inference: str = ""
    gateway: str = ""
    agents: list[str] = field(default_factory=list)
    agent_image: str = ""
    benchmark: str = ""
    datasets: list[dict[str, Any]] = field(default_factory=list)  # {path, n_tasks, task_names}
    tasks_total: int | None = None  # после материализации
    node_pool: str = ""
    agents_per_replica: int = 0
    timeouts: str = ""
    s3: str = ""
    pending_dir: str = ""
    session: str = ""  # id сессии, если бенч на ней

    @property
    def gpu_total(self) -> int | None:
        return self.replicas * self.gpu_per_instance if self.gpu_per_instance is not None else None

    @property
    def agent_limit(self) -> int:
        return self.replicas * self.agents_per_replica

    def rows(self) -> list[tuple[str, str]]:
        gpu = f" = {self.gpu_total} GPU" if self.gpu_total is not None else " (GPU на инстанс не распознан)"
        tasks = f"{self.tasks_total} задач" if self.tasks_total is not None else "число задач — после материализации"
        datasets = "; ".join(
            d["path"] + (f" (первые {d['n_tasks']})" if d.get("n_tasks") else "") + (f" [{len(d['task_names'])} по именам]" if d.get("task_names") else "")
            for d in self.datasets
        )
        model_src = f"сессия {self.session}" if self.session else "поднимет сервис"
        return [
            ("Ревизия fleet", self.revision),
            ("Кластер", self.cluster),
            ("Модель", f"{self.model} · образ {self.model_image or '?'} · очередь {self.queue or '?'} · {model_src}"),
            ("GPU", f"{self.replicas} × {self.instance_type or '?'}{gpu}"),
            ("Инференс", f"{self.inference} · {self.gateway}"),
            ("Агент", f"{', '.join(self.agents) or '?'} · раннер {self.agent_image or '?'}"),
            ("Задачи", f"{self.benchmark}: {datasets} · {tasks} · пул узлов {self.node_pool}"),
            ("Параллелизм", f"лимит {self.agent_limit} = {self.replicas} реплик × {self.agents_per_replica} агентов"),
            ("Таймауты", self.timeouts),
            ("Результаты", self.s3),
            ("Очередь сабмиттера", self.pending_dir),
        ]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "Preview":
        data = json.loads(text or "{}")
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__}) if data else cls()


def tasks_total(run_dir: Path) -> int | None:
    """Сколько пар агент×задача материализовано: out/.fleet/tasks.json."""
    path = run_dir / "out" / ".fleet" / "tasks.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return len(data) if isinstance(data, list) else None


def from_resolved(
    resolved: dict[str, Any],
    *,
    revision: str = "",
    context: str = "",
    run_dir: Path | None = None,
    session: str = "",
) -> Preview:
    """Собрать карточку из итогового конфига; недостающие поля — пустые, а не исключение."""
    job = _get(resolved, "mlsc", "create_job", "job", default={}) or {}
    experiment = resolved.get("experiment") or {}
    harbor_job = resolved.get("harbor_job") or {}
    execution = resolved.get("execution") or {}
    system = resolved.get("system") or {}
    namespace = str(system.get("namespace") or "")
    nodes = _get(system, "polar", "topology", "gateway", "nodes", default=[]) or []
    gateway = _get(nodes[0], "inference", "base_url", default="") if nodes else ""
    selector = _get(system, "workflow_builder", "node_selector", default={}) or {}
    datasets = []
    for d in harbor_job.get("datasets") or []:
        if isinstance(d, dict):
            datasets.append({"path": str(d.get("path", "")), "n_tasks": d.get("n_tasks"), "task_names": list(d.get("task_names") or [])})
    multipliers = [
        f"агент ×{harbor_job.get('agent_timeout_multiplier', 1.0)}",
        f"верификатор ×{harbor_job.get('verifier_timeout_multiplier', 1.0)}",
        f"дедлайн пода {execution.get('active_deadline_seconds', '?')} с",
        f"retry Argo {execution.get('retry_limit', '?')}",
    ]
    return Preview(
        run_id=str(experiment.get("run_id") or ""),
        revision=revision,
        cluster=f"{context or '?'} · {namespace}",
        namespace=namespace,
        model=str(experiment.get("model_name") or _get(resolved, "mlsc", "served_model_name", default="")),
        model_image=str(job.get("image") or ""),
        instance_type=str(job.get("instance_type_name") or ""),
        queue=str(job.get("queue_name") or ""),
        replicas=int(experiment.get("jobs") or 0),
        gpu_per_instance=gpu_in(job.get("instance_type_name")),
        inference=str(experiment.get("inference") or "litellm"),
        gateway=str(gateway),
        agents=[str(a.get("name", "?")) for a in harbor_job.get("agents") or [] if isinstance(a, dict)],
        agent_image=str(system.get("agent_image") or ""),
        benchmark=str(_get(resolved, "harbor_dataset", "benchmark", default="") or ""),
        datasets=datasets,
        tasks_total=tasks_total(run_dir) if run_dir else None,
        node_pool=", ".join(f"{k}={v}" for k, v in selector.items()) or "?",
        agents_per_replica=int(experiment.get("agents_per_replica") or 0),
        timeouts=" · ".join(multipliers),
        s3=str(system.get("s3_base_directory") or ""),
        pending_dir=str(system.get("pending_dir") or ""),
        session=session,
    )
