"""Фейковый контекст для UI-тестов: без git, uv, kubectl и сети."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace

import cluster_samples
import fake_gateway
from fleet_console.connect.kube import KubeStatus
from fleet_console.connect.supervisor import Forward, ForwardState
from fleet_console.context import AppContext
from fleet_console.fleetref.git import Branch, Commit, CommitInfo
from fleet_console.fleetref.manager import PrepareError, Progress, ReadyRevision
from fleet_console.fleetref.profile import CONFIGS_REL, Profile
from fleet_console.guard import SleepGuard
from fleet_console.models import SessionManager
from fleet_console.observe import ClusterObserver
from fleet_console.runner import FakeRunner, Result
from fleet_console.runs import BenchManager
from fleet_console.settings import Paths, Settings
from fleet_console.store import Store

CTX: AppContext | None = None
NOW = datetime(2026, 10, 21, 6, 0, tzinfo=timezone.utc)
SHA_MASTER = "378874ba717f0fc37c2df6551351fa4d4e549eee"
SHA_FEAT = "de460aa21dd934bfe948ef4b1ac73395872234cf"
GROUPS = {"mlsc/model": ["glm"], "harbor/agent": ["terminus2"], "harbor/dataset": ["cowork"], "execution": ["production"], "datasets": ["cowork"]}
STEPS = [
    "system.materialize", "system.topology_saver", "system.manifest_runner", "mlsc.create_agent_limit", "system.runtime_upload",
    "system.task_artifacts_upload", "system.pod_upload", "system.submitter_launch", "system.measure_run", "system.requeue_failed",
    "mlsc.create_job", "mlsc.start_llm", "mlsc.check_llm_works", "mlsc.stop_llm",
]
DATASETS_ROOT = Path(tempfile.mkdtemp(prefix="fleet-console-ui-data-"))
RESOLVED = f"""system:
  namespace: swe-bench-v2-20260308
  agent_image: registry/gc-cli-harbor:v0.3.0
  s3_base_directory: s3://p-rndml-gcvmfleet/swe-bench-v2-20260308/GLM/bench
  pending_dir: /manifests/harbor_run/bench/pending
  polar:
    topology:
      gateway:
        nodes:
          - inference:
              base_url: http://litellm.litellm-argo
  workflow_builder:
    node_selector:
      type: bench
mlsc:
  served_model_name: GLM
  create_job:
    job:
      image: fleet-vllm:0.24.0
      instance_type_name: a100plus.8gpu.80vG.96C.1456G
      queue_name: gigacode
experiment:
  run_id: bench
  model_name: GLM
  jobs: 1
  agents_per_replica: 2
  inference: litellm
harbor_job:
  agents:
    - name: terminus-2
  datasets:
    - path: {DATASETS_ROOT}/cowork/tasks
      n_tasks: 3
  agent_timeout_multiplier: 2.0
  verifier_timeout_multiplier: 2.0
harbor_dataset:
  benchmark: cowork
execution:
  active_deadline_seconds: 7200
  retry_limit: 1
"""


class FakeKube:
    async def status(self, checks=None) -> KubeStatus:
        return KubeStatus(context="internal", server="https://127.0.0.1:8443", allowed=True, ready=True)

    async def readyz(self) -> bool:
        return True


class FakeProc:
    def __init__(self) -> None:
        self.returncode: int | None = None

    def kill(self) -> None:
        self.returncode = -9


async def fake_spawner(argv, log_path) -> FakeProc:
    return FakeProc()


class FakeSupervisor:
    def __init__(self) -> None:
        async def ok() -> bool:
            return True

        self.forwards = [Forward("bastion", "бастион", ["ssh", "-N", "fleetbastion"], ok, hint="ssh -N fleetbastion")]
        self.states = {"bastion": ForwardState(status="green")}
        self.restarted: list[str] = []

    def state(self, name: str) -> ForwardState:
        return self.states[name]

    def forward(self, name: str) -> Forward:
        return next(f for f in self.forwards if f.name == name)

    def log_path(self, fwd: Forward) -> Path | None:
        return None

    def add(self, fwd: Forward) -> None:
        if fwd.name in self.states:
            raise ValueError(fwd.name)
        self.forwards.append(fwd)
        self.states[fwd.name] = ForwardState(status="green")  # в симуляции проброс поднимается мгновенно

    def remove(self, name: str) -> None:
        self.states.pop(name, None)
        self.forwards = [f for f in self.forwards if f.name != name]

    async def run(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def restart(self, name: str) -> None:
        self.restarted.append(name)


class FakeManager:
    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree
        self.progress = Progress()
        self.current: ReadyRevision | None = None
        self.prepare_error: str | None = None
        self.prepared: list[str] = []
        self.fetched = 0
        self.mirror = SimpleNamespace(exists=lambda: True, last_fetch_at=lambda: NOW, remote="fake")
        self.files_at: dict[tuple[str, str], str] = {}  # (sha, путь группы) → текст на той ревизии

    async def file_at(self, sha: str, rel: str) -> str | None:
        return self.files_at.get((sha, rel))

    async def ensure_mirror(self) -> None:
        pass

    async def gc_worktrees(self, ttl_days=None) -> list[str]:
        return []

    async def fetch(self) -> None:
        self.fetched += 1

    async def branches(self) -> list[Branch]:
        return [Branch("master", SHA_MASTER, NOW, "egorbu"), Branch("feat/x", SHA_FEAT, NOW, "Dasha")]

    async def commits(self, ref: str, limit: int = 50) -> list[Commit]:
        sha = SHA_FEAT if ref == "feat/x" else SHA_MASTER
        return [Commit(sha, NOW, "Egor", f"last commit on {ref}"), Commit("1" * 40, NOW, "Egor", "older")]

    async def commit_info(self, ref: str) -> CommitInfo:
        if ref not in {"master", "feat/x", SHA_MASTER, SHA_FEAT, "1" * 40}:
            raise RuntimeError("unknown revision")
        feat = ref in {"feat/x", SHA_FEAT}
        sha = SHA_FEAT if feat else SHA_MASTER
        return CommitInfo(sha, NOW, "Dasha" if feat else "egorbu", "subject", merged_into_main=not feat, behind_main=3 if feat else 0)

    def is_ready(self, sha: str) -> bool:
        return self.current is not None and self.current.info.sha == sha

    async def prepare(self, ref: str) -> ReadyRevision:
        self.prepared.append(ref)
        if self.prepare_error:
            self.progress.fail(self.prepare_error)
            raise PrepareError(self.prepare_error)
        info = await self.commit_info(ref)
        profile = Profile(
            sha=info.sha,
            layout="harbor_new",
            groups=GROUPS,
            cli_flags=["--print-config"],
            print_config=True,
            steps=STEPS,
            harbor_version="0.22.0",
            agent_image="registry/gc-cli-harbor:v0.3.0",
        )
        self.progress.phase = "done"
        self.current = ReadyRevision(ref, info, self.worktree, profile, from_cache=False)
        return self.current


def make_worktree(root: Path) -> Path:
    cfg = root / CONFIGS_REL
    files = {
        "harbor/agent/terminus2.yaml": "# @package _global_\n# Агент terminus-2 ходит в Polar gateway.\nharbor_job:\n  agents:\n    - name: terminus-2\n",
        "mlsc/model/glm.yaml": "# @package mlsc\n# Рецепт GLM-5.3-Flash на 8 GPU.\nserved_model_name: GLM\ncreate_job:\n  job:\n    instance_type_name: a100plus.8gpu.80vG\n",
        "datasets/cowork.yaml": "benchmark: cowork\nsource:\n  tasks_root: cowork/tasks\nruntime:\n  task_s3_base: cowork/tasks\n",
        "harbor/dataset/cowork.yaml": "# @package _global_\n# Cowork bench v2.\nharbor_job:\n  datasets:\n    - path: ${system.datasets_root}/cowork/tasks\nharbor_dataset:\n  benchmark: cowork\n",
        "system/cluster.yaml": "# Стенд.\nnamespace: swe-bench-v2-20260308\n",
        "mlsc/common.yaml": "pod_name: litellm-noauth-abc\n",
        "execution/production.yaml": "task_limit: null\n",
        "harbor/job/default.yaml": "harbor_job: {}\n",
        "harbor/source/composed.yaml": "harbor_source: {}\n",
    }
    (DATASETS_ROOT / "cowork" / "tasks").mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (cfg / rel).parent.mkdir(parents=True, exist_ok=True)
        (cfg / rel).write_text(text, encoding="utf-8")
    return root


def fake_fleet(argv: tuple[str, ...]) -> Result:
    """`uv run … fleet`: create_job пишет version_id, materialize — tasks.json, --print-config отдаёт конфиг; остальное успешно."""
    overrides = {b.split("=", 1)[0]: b.split("=", 1)[1] for a, b in zip(argv, argv[1:]) if a == "-o" and "=" in b}
    if "--print-config" in argv:
        return Result(argv, 0, RESOLVED)
    steps = overrides.get("main.run", "[]").strip("[]").split(",")
    if steps[0] == "mlsc.create_job":
        Path(overrides["mlsc.version_id_file"]).write_text("777\n", encoding="utf-8")
    if steps[0] == "system.materialize" and "output_dir" in overrides:
        out = Path(overrides["output_dir"]) / ".fleet"
        out.mkdir(parents=True, exist_ok=True)
        (out / "tasks.json").write_text("[{}, {}, {}]", encoding="utf-8")
        return Result(argv, 0, "Материализовано Harbor-пар агент×задача: 3\n")
    return Result(argv, 0, "ok\n")


def fake_submitter(store: Store):
    """Сабмиттер: строки образца плюс «готово» по каждому бенчу из журнала — так прогон в симуляции доходит до замера."""

    def handler(argv: tuple[str, ...]) -> Result:
        rows = [f"{r.id}\t0\t0\t3\t3\t0\t1761022800" for r in store.benches()]
        return Result(argv, 0, cluster_samples.SUBMITTER_TEXT + "".join(row + "\n" for row in rows))

    return handler


def make_context() -> AppContext:
    root = Path(tempfile.mkdtemp(prefix="fleet-console-ui-test-"))
    paths = Paths(root / "home").ensure()
    settings = Settings()
    settings.datasets_root = str(DATASETS_ROOT)
    settings.bench.poll_seconds = 0
    settings.bench.startup_grace_seconds = 0
    settings.bench.requeue_rounds = 0
    store = Store(paths.db)
    kubectl = FakeRunner()
    kubectl.on("kubectl", "exec", "-n", settings.namespace, "fleet-submitter-python", "--", "sh", result=fake_submitter(store))
    cluster_samples.install(kubectl, settings.namespace)
    kubectl.on("uv", "run", result=fake_fleet)
    kubectl.on("osascript")
    manager = FakeManager(make_worktree(root / "wt"))
    observer = ClusterObserver(kubectl, settings, mlsc_token=lambda: "token")
    sessions = SessionManager(store, kubectl, paths, settings, token=lambda: "token", mlsc_pod=observer.mlsc_pod, platform="linux")
    # профиль «ревизии» FakeManager — в кеш, чтобы SessionManager.spec находил заглушки harbor
    store.upsert_revision(SHA_MASTER, Profile(sha=SHA_MASTER, layout="harbor_new", groups=GROUPS, steps=STEPS).to_dict(), "master")
    secrets = {"MLSC_TOKEN": "token", "AWS_ACCESS_KEY_ID_gcvmfleet": "ak", "AWS_SECRET_ACCESS_KEY_gcvmfleet": "sk"}
    ctx = AppContext(
        settings=settings,
        paths=paths,
        store=store,
        kube=FakeKube(),  # type: ignore[arg-type]
        manager=manager,  # type: ignore[arg-type]
        supervisor=FakeSupervisor(),  # type: ignore[arg-type]
        observer=observer,
        http=fake_gateway.client(),
        port_picker=lambda preferred: preferred,  # порт из настроек, без проверки занятости
        guard=SleepGuard(kubectl, fake_spawner, platform="darwin", pid=1),
        sessions=sessions,
    )

    async def fast_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    ctx.benches = BenchManager(
        store, kubectl, paths, settings, secrets=lambda: secrets, mlsc_pod=observer.mlsc_pod, observer=observer, sessions=sessions,
        agent_limit=ctx.set_agent_limit, stop_tasks=lambda run_id: ctx.stop_own_run(run_id), sleep=fast_sleep, platform="linux",
    )
    sessions.paused = ctx.benches.busy_sessions
    return ctx
