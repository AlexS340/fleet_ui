"""Скачивание датасета Harbor из S3-зеркала fleet: `aws s3 sync` по реестру datasets.

Зеркало: `${system.s3_datasets_root}/<runtime.task_s3_base>/<задача>/…` (на кластере поды читают его
же); локально fleet ждёт `${FLEET_DATASETS_ROOT}/<source.tasks_root>/<задача>/` с task.toml,
environment/ и tests/. Можно скачать все задачи, задачи по именам из конфига или первые N — для
smoke целый датасет не нужен.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping as MappingT
from dataclasses import dataclass, field
from pathlib import Path
import shlex

from fleet_console.compose.registry import Mapping
from fleet_console.runner import CommandError, Runner

PRE = "PRE "


@dataclass(frozen=True)
class FetchPlan:
    name: str  # маппинг реестра
    src: str  # s3://…/harbor_datasets/<task_s3_base>/
    dst: Path  # <datasets_root>/<tasks_root>
    tasks: tuple[str, ...] = ()  # только эти задачи (имена каталогов, можно glob); пусто — весь датасет

    @property
    def argv(self) -> list[str]:
        args = ["aws", "s3", "sync", self.src, str(self.dst)]
        if self.tasks:
            args += ["--exclude", "*"]
            for task in self.tasks:
                args += ["--include", f"{task}/*"]
        return args

    @property
    def command(self) -> str:
        return shlex.join(self.argv)


def plan_fetch(mapping: Mapping, datasets_root: Path, s3_datasets_root: str, tasks: tuple[str, ...] = ()) -> FetchPlan:
    src = f"{s3_datasets_root.rstrip('/')}/{mapping.task_s3_base.strip('/')}/"
    root = Path(mapping.tasks_root)
    dst = root if root.is_absolute() else datasets_root / mapping.tasks_root
    return FetchPlan(mapping.name, src, dst, tuple(tasks))


def parse_prefixes(text: str) -> list[str]:
    """`aws s3 ls <префикс>/` без --recursive: строки `PRE <имя>/` — каталоги задач."""
    names = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(PRE):
            names.append(line[len(PRE) :].rstrip("/"))
    return sorted(names)


async def list_task_dirs(runner: Runner, src: str, env: MappingT[str, str]) -> list[str]:
    result = await runner.run(["aws", "s3", "ls", src.rstrip("/") + "/"], env=env, timeout=120)
    if not result.ok:
        raise CommandError(result)
    return parse_prefixes(result.stdout)


@dataclass
class FetchJob:
    plan: FetchPlan
    log: Path
    status: str = "running"  # running | done | failed
    returncode: int | None = None
    lines: int = 0
    last: str = ""
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def active(self) -> bool:
        return self.status == "running"


class DatasetFetcher:
    """Скачивания в фоне: одно на каталог назначения, лог в logs/datasets/<маппинг>.log."""

    def __init__(self, runner: Runner, logs_dir: Path, env: Callable[[], dict[str, str]], event: Callable[[str, str, str, str], None] | None = None) -> None:
        self.runner = runner
        self.logs_dir = logs_dir
        self.env = env
        self.event = event
        self.jobs: dict[str, FetchJob] = {}  # по каталогу назначения

    async def task_dirs(self, src: str) -> list[str]:
        return await list_task_dirs(self.runner, src, self.env())

    def job_for(self, dst: Path) -> FetchJob | None:
        return self.jobs.get(str(dst))

    def fetch(self, plan: FetchPlan) -> FetchJob:
        current = self.job_for(plan.dst)
        if current is not None and current.active:
            raise RuntimeError(f"в {plan.dst} уже идёт скачивание")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        job = FetchJob(plan, self.logs_dir / f"{plan.name}.log")
        self.jobs[str(plan.dst)] = job
        job.task = asyncio.create_task(self._run(job), name=f"dataset-{plan.name}")
        return job

    async def _run(self, job: FetchJob) -> None:
        def on_line(line: str) -> None:
            job.lines += 1
            job.last = line.strip()

        try:
            job.plan.dst.mkdir(parents=True, exist_ok=True)
            result = await self.runner.stream(job.plan.argv, env=self.env(), log_path=job.log, on_line=on_line)
            job.returncode = result.returncode
            job.status = "done" if result.ok else "failed"
            if not result.ok and result.stderr.strip():
                job.last = result.stderr.strip().splitlines()[-1]
        except Exception as exc:  # noqa: BLE001 — нет aws или сломался процесс: показываем в диалоге
            job.status, job.last = "failed", str(exc)
        if self.event:
            self.event("dataset_fetch", job.plan.name, "ok" if job.status == "done" else "error", f"{job.plan.command} → {job.plan.dst} ({job.last})")
