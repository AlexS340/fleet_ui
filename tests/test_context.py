from pathlib import Path

from fleet_console.connect.kube import KubeStatus
from fleet_console.context import AppContext
from fleet_console.observe import ClusterObserver
from fleet_console.runner import FakeRunner
from fleet_console.settings import Paths, Settings
from fleet_console.store import Store
from fakes import FakeManager, FakeSupervisor, make_worktree


class ScriptedKube:
    """Первый ответ с правами — «get nodes» ✗ (транзитный сбой), дальше всё ✓."""

    def __init__(self) -> None:
        self.calls: list[bool] = []

    down = False

    async def status(self, checks=None) -> KubeStatus:
        with_perms = checks is not None
        self.calls.append(with_perms)
        if self.down:
            return KubeStatus(context="internal", server="s", allowed=True, ready=False, error="API не отвечает")
        st = KubeStatus(context="internal", server="s", allowed=True, ready=True)
        if with_perms:
            first = self.calls.count(True) == 1
            st.permissions = {"get nodes": not first, "list workflows.argoproj.io -n ns": True}
            if st.missing_permissions:
                st.error = "нет прав: get nodes"
        return st


def _ctx(tmp_path: Path) -> AppContext:
    paths = Paths(tmp_path / "home").ensure()
    settings = Settings()
    observer = ClusterObserver(FakeRunner(), settings, mlsc_token=lambda: None)
    return AppContext(settings, paths, Store(paths.db), ScriptedKube(), FakeManager(make_worktree(tmp_path / "wt")), FakeSupervisor(), observer)  # type: ignore[arg-type]


async def test_permissions_persist_and_recheck_until_green(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert ctx.needs_permission_check()
    await ctx.refresh_kube(with_permissions=True)
    assert ctx.kube_status.missing_permissions == ["get nodes"] and ctx.needs_permission_check()
    await ctx.refresh_kube(with_permissions=False)  # обычный опрос не должен «терять» права
    assert ctx.kube_status.missing_permissions == ["get nodes"] and "нет прав" in ctx.kube_status.error
    await ctx.refresh_kube(with_permissions=ctx.needs_permission_check())
    assert ctx.kube_status.missing_permissions == [] and not ctx.needs_permission_check()
    await ctx.refresh_kube(with_permissions=False)
    assert ctx.kube_status.permissions and ctx.kube_status.error == ""


async def test_permissions_survive_api_outage(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    await ctx.refresh_kube(with_permissions=True)
    await ctx.refresh_kube(with_permissions=True)
    assert ctx.kube_status.missing_permissions == []
    ctx.kube.down = True  # type: ignore[attr-defined]
    await ctx.refresh_kube(with_permissions=ctx.needs_permission_check())
    assert not ctx.kube_status.ready and ctx.kube_status.permissions  # обрыв связи не стирает известные права
    assert "API не отвечает" in ctx.kube_status.error
