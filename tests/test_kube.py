from fleet_console.connect.kube import Kube, required_checks
from fleet_console.runner import FakeRunner, Result


def _runner(context="internal", ready=True, can_i="yes") -> FakeRunner:
    fake = FakeRunner()
    fake.on("kubectl", "config", "current-context", stdout=f"{context}\n")
    fake.on("kubectl", "config", "view", stdout='{"clusters":[{"cluster":{"server":"https://127.0.0.1:8443"}}]}')
    fake.on("kubectl", "get", "--raw", "/readyz", result=Result(("x",), 0 if ready else 1, "ok" if ready else "", "refused"))
    fake.on("kubectl", "auth", "can-i", stdout=f"{can_i}\n")
    return fake


async def test_status_ok_with_permissions() -> None:
    kube = Kube(_runner(), ["internal"])
    st = await kube.status(required_checks("ns", "argo"))
    assert st.context == "internal" and st.server == "https://127.0.0.1:8443"
    assert st.allowed and st.ready and st.error == ""
    assert st.permissions["list workflows.argoproj.io -n ns"] is True and st.missing_permissions == []
    assert "get nodes" in st.permissions


async def test_status_context_not_allowed_stops_early() -> None:
    fake = _runner(context="prod")
    st = await Kube(fake, ["internal"]).status(required_checks("ns", "argo"))
    assert not st.allowed and "не в списке разрешённых" in st.error
    assert not any(c[0][:2] == ("kubectl", "get") for c in fake.calls)  # readyz не вызывался


async def test_status_api_down_and_missing_rights() -> None:
    st = await Kube(_runner(ready=False), ["internal"]).status()
    assert st.allowed and not st.ready and "/readyz" in st.error
    st = await Kube(_runner(can_i="no"), ["internal"]).status([("get", "nodes", None)])
    assert st.ready and st.missing_permissions == ["get nodes"] and "нет прав" in st.error


async def test_status_no_context() -> None:
    fake = FakeRunner()
    fake.on("kubectl", result=Result(("kubectl",), 1, "", "error"))
    st = await Kube(fake, ["internal"]).status()
    assert st.context is None and "kubeconfig" in st.error
