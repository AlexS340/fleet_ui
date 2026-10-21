"""Фейковый контекст для UI-тестов: без git, uv, kubectl и сети."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace

from fleet_console.connect.kube import KubeStatus
from fleet_console.connect.supervisor import Forward, ForwardState
from fleet_console.context import AppContext
from fleet_console.fleetref.git import Branch, Commit, CommitInfo
from fleet_console.fleetref.manager import PrepareError, Progress, ReadyRevision
from fleet_console.fleetref.profile import CONFIGS_REL, Profile
from fleet_console.settings import Paths, Settings
from fleet_console.store import Store

CTX: AppContext | None = None
NOW = datetime(2026, 10, 21, 6, 0, tzinfo=timezone.utc)
SHA_MASTER = "378874ba717f0fc37c2df6551351fa4d4e549eee"
SHA_FEAT = "de460aa21dd934bfe948ef4b1ac73395872234cf"


class FakeKube:
    async def status(self, checks=None) -> KubeStatus:
        return KubeStatus(context="internal", server="https://127.0.0.1:8443", allowed=True, ready=True)

    async def readyz(self) -> bool:
        return True


class FakeSupervisor:
    def __init__(self) -> None:
        async def ok() -> bool:
            return True

        self.forwards = [
            Forward("bastion", "бастион", ["ssh", "-N", "fleetbastion"], ok, hint="ssh -N fleetbastion"),
            Forward("argo", "Argo", ["kubectl", "port-forward"], ok, depends_on="bastion"),
        ]
        self.states = {"bastion": ForwardState(status="green"), "argo": ForwardState(status="off", note="ждёт: бастион")}
        self.restarted: list[str] = []

    def state(self, name: str) -> ForwardState:
        return self.states[name]

    def forward(self, name: str) -> Forward:
        return next(f for f in self.forwards if f.name == name)

    def log_path(self, fwd: Forward) -> Path | None:
        return None

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
            groups={"mlsc/model": ["glm"]},
            cli_flags=["--print-config"],
            print_config=True,
            steps=["mlsc.create_job", "system.materialize"],
            harbor_version="0.22.0",
            agent_image="registry/gc-cli-harbor:v0.3.0",
        )
        self.progress.phase = "done"
        self.current = ReadyRevision(ref, info, self.worktree, profile, from_cache=False)
        return self.current


def make_worktree(root: Path) -> Path:
    cfg = root / CONFIGS_REL
    files = {
        "harbor/agent/terminus2.yaml": "# @package _global_\n# Агент terminus-2 ходит в Polar gateway.\nharbor_job: {}\n",
        "mlsc/model/glm.yaml": "# @package mlsc\n# Рецепт GLM-5.3-Flash на 8 GPU.\nserved_model_name: GLM\n",
        "datasets/cowork.yaml": "benchmark: cowork\nsource:\n  tasks_root: cowork/tasks\n",
    }
    for rel, text in files.items():
        (cfg / rel).parent.mkdir(parents=True, exist_ok=True)
        (cfg / rel).write_text(text, encoding="utf-8")
    return root


def make_context() -> AppContext:
    root = Path(tempfile.mkdtemp(prefix="fleet-console-ui-test-"))
    paths = Paths(root / "home").ensure()
    settings = Settings()
    return AppContext(
        settings=settings,
        paths=paths,
        store=Store(paths.db),
        kube=FakeKube(),  # type: ignore[arg-type]
        manager=FakeManager(make_worktree(root / "wt")),  # type: ignore[arg-type]
        supervisor=FakeSupervisor(),  # type: ignore[arg-type]
    )
