from pathlib import Path

import pytest

from fleet_console.fleetref.git import Mirror
from fleet_console.fleetref.manager import PrepareError, RevisionManager
from fleet_console.runner import FakeRunner, Result, SubprocessRunner
from fleet_console.settings import Paths, Settings
from fleet_console.store import Store
from test_profile import HELP, RESOLVED


class RoutingRunner:
    """git — по-настоящему (локально), uv/fleet — FakeRunner."""

    def __init__(self) -> None:
        self.real = SubprocessRunner()
        self.fake = FakeRunner()

    def _pick(self, argv):
        return self.real if argv[0] == "git" else self.fake

    async def run(self, argv, **kw):
        return await self._pick(argv).run(argv, **kw)

    async def stream(self, argv, **kw):
        return await self._pick(argv).stream(argv, **kw)


def make_venv(worktree: Path) -> None:
    (worktree / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (worktree / ".venv" / "bin" / "fleet").touch()


@pytest.fixture
def manager(remote_repo: Path, paths: Paths):
    runner = RoutingRunner()
    settings = Settings(fleet_remote=str(remote_repo))
    mirror = Mirror(runner, paths, settings.fleet_remote)
    store = Store(paths.db)
    gate = {"reason": None}
    mgr = RevisionManager(mirror, store, runner, paths, settings, sync_gate=lambda: gate["reason"])
    mgr.gate = gate  # type: ignore[attr-defined]
    runner.fake.on("uv", "run", "--project", result=lambda argv: Result(argv, 0, HELP if "--help" in argv else RESOLVED))

    def sync(argv):
        wt = runner.fake.calls[-1][1]["cwd"]
        make_venv(wt)
        return Result(argv, 0, "Resolved\nInstalled 3 packages")

    runner.fake.on("uv", "sync", "--frozen", result=sync)
    return mgr


async def test_prepare_syncs_builds_profile_then_uses_cache(manager: RevisionManager) -> None:
    await manager.ensure_mirror()
    assert manager.progress.phase == "done" and manager.mirror.exists()
    ready = await manager.prepare("feat/x")
    assert ready.info.author == "Dasha" and ready.worktree.exists() and not ready.from_cache
    assert ready.profile.layout == "legacy" and ready.profile.harbor_version is None  # мини-репозиторий без configs
    assert manager.is_ready(ready.info.sha) and manager.progress.phase == "done"
    sync_calls = [c for c in manager.runner.fake.calls if c[0][:2] == ("uv", "sync")]
    assert len(sync_calls) == 1 and sync_calls[0][1]["env"]["HTTPS_PROXY"].startswith("socks5h://")
    assert "Installed 3 packages" in list(manager.progress.log)

    again = await manager.prepare("feat/x")
    assert again.from_cache and again.info.sha == ready.info.sha
    assert len([c for c in manager.runner.fake.calls if c[0][:2] == ("uv", "sync")]) == 1
    actions = [e.action for e in manager.store.events()]
    assert actions.count("prepare") == 2 and "clone" in actions


async def test_prepare_refuses_sync_when_gate_closed(manager: RevisionManager) -> None:
    await manager.ensure_mirror()
    manager.gate["reason"] = "бастион не зелёный"  # type: ignore[attr-defined]
    with pytest.raises(PrepareError, match="бастион"):
        await manager.prepare("master")
    assert manager.progress.phase == "error" and "бастион" in manager.progress.error
    assert manager.store.events()[0].result == "error"


async def test_prepare_sync_failure_and_bad_ref(manager: RevisionManager) -> None:
    await manager.ensure_mirror()
    manager.runner.fake.handlers = [h for h in manager.runner.fake.handlers if h[0][:2] != ("uv", "sync")]
    manager.runner.fake.on("uv", "sync", result=lambda argv: Result(argv, 1, "error: timed out"))
    with pytest.raises(PrepareError, match="кодом 1"):
        await manager.prepare("master")
    with pytest.raises(PrepareError):
        await manager.prepare("no-such-branch")
    assert not manager.progress.busy


async def test_fetch_and_gc(manager: RevisionManager) -> None:
    await manager.ensure_mirror()
    await manager.fetch()
    assert manager.mirror.last_fetch_at() is not None
    ready = await manager.prepare("master")
    assert await manager.gc_worktrees() == []  # свежий
    assert await manager.gc_worktrees(ttl_days=0) == [ready.info.sha]
    assert manager.mirror.worktrees() == [] and manager.store.get_revision(ready.info.sha) is None


async def test_restore_last_revision_after_restart(manager: RevisionManager) -> None:
    await manager.ensure_mirror()
    assert await manager.restore() is None  # ничего не готовили
    ready = await manager.prepare("master")
    manager.current = None  # «перезапуск»
    restored = await manager.restore()
    assert restored is not None and restored.info.sha == ready.info.sha and restored.from_cache and manager.current is restored
    assert await manager.restore() is restored  # повторно — без работы
