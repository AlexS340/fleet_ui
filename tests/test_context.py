from pathlib import Path

import cluster_samples
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


async def test_gateway_open_close(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.port_picker = lambda preferred: preferred
    gw = ctx.open_gateway("litellm")
    assert gw.base_url == "http://127.0.0.1:18080/v1" and gw.argv[-2:] == ["service/litellm-fleet", "18080:80"]
    assert ctx.open_gateway("litellm") is gw  # повторно — тот же проброс
    fwd = ctx.supervisor.forward("gateway-litellm")
    assert fwd.depends_on == "bastion" and "kubectl -n litellm-argo port-forward" in fwd.hint
    ctx.close_gateway("litellm")
    assert all(f.name != "gateway-litellm" for f in ctx.supervisor.forwards) and not ctx.gateways
    ctx.close_gateway("litellm")  # повторно — тихо
    assert [e.action for e in ctx.store.events()] == ["close_gateway", "open_gateway"]


async def test_sleep_reasons_cover_only_active_own_runs(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    cluster_samples.install(ctx.observer.runner)
    await ctx.observer.refresh()
    assert ctx.sleep_reasons() == {}  # чужие прогоны Mac не держат
    ctx.store.adopt_run("run-a")
    ctx.store.adopt_run("run-b")  # завершён: активных задач нет
    assert list(ctx.sleep_reasons()) == ["run-a"] and "задач идёт 2" in ctx.sleep_reasons()["run-a"]


async def test_control_actions_are_journaled(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    fake = ctx.observer.runner
    cluster_samples.install(fake)
    fake.on("kubectl", "-n", ctx.settings.namespace, "patch", "workflow")
    cmd = await ctx.set_agent_limit("run-a", 20)
    assert "'20' > /manifests/harbor_run/run-a/agents_limit" in cmd.argv[-1] and cmd.text.startswith("kubectl exec")
    report = await ctx.stop_own_run("run-a", version_id=41)  # токена нет: задачи остановлены, модель — ошибка
    assert report.terminated == 2 and any("MLSC_TOKEN" in f for f in report.failed) and not report.ok
    ctx.observer.mlsc_token = lambda: "t"
    assert await ctx.set_replicas(41, 2) == cluster_samples.CONFIGURATION  # фейк отвечает тем же JSON на любой exec -i
    actions = [(e.action, e.result) for e in ctx.store.events()]
    assert actions == [("set_replicas", "ok"), ("stop_run", "error"), ("set_replicas", "error"), ("agent_limit", "ok")]
