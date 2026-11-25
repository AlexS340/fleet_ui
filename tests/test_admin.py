import asyncio
from datetime import UTC, datetime

import fakes
import pytest
from nicegui import core, ui
from nicegui.testing import User

from fleet_console.auth import Access, AccessDenied, Identity, TestIdentityAdapter as LoginAdapter, socket_allowed
from fleet_console.settings import Quotas
from fleet_console.store import BenchRun
from fleet_console.store.db import Store
from fleet_console.ui.measurements import issue_link

pytestmark = pytest.mark.nicegui_main_file("tests/auth_ui_main.py")


async def login(user: User, name: str, password: str | None = None):
    return await user.http_client.post("/test-login", data={"user": name, "password": password or f"{name}-test"})


def pick(user: User, label: str, value) -> None:
    field = next(item for item in user.find(kind=ui.select).elements if item.props.get("label") == label)
    with user.client:
        field.set_value(value)


async def test_registration_requires_admin_approval(user: User) -> None:
    store = fakes.CTX.store
    client = user.http_client
    password = "chosen-test-password"
    data = {"user": "new-user", "password": password, "role": "admin", "enabled": "1"}
    assert "Зарегистрироваться" in (await client.get("/test-login")).text
    responses = await asyncio.gather(*(client.post("/test-register", data=data) for _ in range(2)))
    assert sorted(r.status_code for r in responses) == [303, 409]
    success = next(r for r in responses if r.status_code == 303)
    assert "после одобрения" in (await client.get(success.headers["location"])).text
    assert LoginAdapter.cookie not in client.cookies
    account = store.user("new-user")
    assert account.role == "user" and account.pending_approval and not account.enabled
    assert password not in store.password_hash(account.id)
    assert (await login(user, account.id, password)).status_code == 401
    assert (await client.get("/measurements")).status_code == 303

    await login(user, "operator")
    await user.open("/admin")
    field = next(item for item in user.find(kind=ui.select).elements if item.props.get("label") == "Пользователь")
    assert "ожидает одобрения" in field.options[account.id]
    pick(user, "Пользователь", account.id)
    await user.should_see("Сохранить пользователя")
    user.find("Разрешить вход", kind=ui.switch).click()
    user.find("Сохранить пользователя").click()
    await user.should_see("Настройки пользователя сохранены")
    assert not store.user(account.id).pending_approval
    assert store.admin_events()[0].actor_id == "operator"
    client.cookies.clear()
    assert (await login(user, account.id, password)).status_code == 303
    assert (await client.get("/measurements")).status_code == 200
    assert (await client.get("/admin")).status_code == 403


async def test_registration_rejects_invalid_or_reserved_credentials(user: User) -> None:
    store = fakes.CTX.store
    store.register_user("existing-without-password")
    digest = store.password_hash("operator")
    client = user.http_client
    for name, password, status in [
        ("", "valid-test-password", 400), ("@local", "valid-test-password", 400),
        ("unused", "short", 400), ("operator", "valid-test-password", 409),
        ("existing-without-password", "valid-test-password", 409),
    ]:
        response = await client.post("/test-register", data={"user": name, "password": password})
        assert response.status_code == status and password not in response.text
    assert store.user("unused") is None and store.user("@local") is None
    assert store.password_hash("existing-without-password") is None
    assert store.password_hash("operator") == digest


async def test_admin_transfers_run_and_assigns_multiple_coauthors(user: User) -> None:
    store = fakes.CTX.store
    alice = Access(store, Identity("alice"))
    bob = Access(store, Identity("bob"))
    store.register_user("cara")
    store.register_user("dana")
    store.adopt_run("alice-cowork", owner_id="alice")
    old_own = store.get_run("alice-cowork")
    token = issue_link(alice, "alice-cowork", 1, datetime.now(UTC))
    client = user.http_client
    assert (await login(user, "alice")).status_code == 303
    assert (await client.get("/admin")).status_code == 403
    assert (await login(user, "operator")).status_code == 303
    await user.open("/admin")
    with user.client:
        next(iter(user.find(kind=ui.tabs).elements)).set_value("Прогоны")
    pick(user, "Прогон", "alice-cowork")
    await user.should_see("Сохранить принадлежность")
    pick(user, "Владелец", "bob")
    pick(user, "Соавторы", ["cara", "dana"])
    user.find("Сохранить принадлежность").click()
    assert store.run_ownership("alice-cowork") == ("bob", ["cara", "dana"])
    assert store.get_run("alice-cowork").owner_id == "bob"
    assert not alice.allows(old_own) and bob.allows(old_own)
    for name in ("bob", "cara", "dana"):
        assert Access(store, Identity(name)).run("alice-cowork")
        assert "alice-cowork" in {row["run_id"] for row in store.measurements(name)}
    with pytest.raises(AccessDenied):
        alice.run("alice-cowork")
    assert store.admin_events()[0].actor_id == "operator"
    assert (await login(user, "alice")).status_code == 303
    assert (await client.get("/runs/alice-cowork")).status_code == 404
    assert (await client.get(f"/measurements/shared/{token}")).status_code == 404
    assert not store.measurements("alice")
    assert (await login(user, "bob")).status_code == 303
    await user.open("/runs/alice-cowork")
    await user.should_see("Владелец: bob · Соавторы: cara, dana")


async def test_admin_resets_password_and_revokes_open_session(user: User, monkeypatch) -> None:
    store = fakes.CTX.store
    client = user.http_client
    assert (await login(user, "bob")).status_code == 303
    old_cookie = client.cookies[LoginAdapter.cookie]
    bob_page = await user.open("/measurements")
    monkeypatch.setattr(core.sio, "get_environ", lambda _: {"asgi.scope": bob_page.request.scope})
    assert socket_allowed("bob", bob_page.id, joined=False)

    assert (await login(user, "operator")).status_code == 303
    await user.open("/admin")
    pick(user, "Пользователь", "bob")
    await user.should_see("Сбросить пароль")
    user.find("Сбросить пароль").click()
    with user.client:
        field = next(item for item in user.find(kind=ui.input).elements if item.props.get("label") == "Новый пароль")
        field.set_value("new-demo-password")
    user.find("Установить новый пароль").click()
    await user.should_see("Пароль изменён. Старые сеансы отозваны")
    assert not socket_allowed("bob", bob_page.id, joined=False)
    client.cookies.clear()
    client.cookies.set(LoginAdapter.cookie, old_cookie)
    assert (await client.get("/measurements")).status_code == 303
    client.cookies.clear()
    assert (await login(user, "bob")).status_code == 401
    assert (await login(user, "bob", "new-demo-password")).status_code == 303

    with_store = Store(store.path)
    try:
        digest = with_store.password_hash("bob")
        LoginAdapter({"bob": "bob-test"}).bind(with_store)
        assert digest == with_store.password_hash("bob") and "new-demo-password" not in digest
        event = with_store.admin_events()[0]
        assert (event.action, event.actor_id, event.object, event.details) == ("admin_password", "operator", "bob", "")
    finally:
        with_store.close()


async def test_admin_saves_account_policy_and_pauses_queued_work(user: User) -> None:
    store = fakes.CTX.store
    client = user.http_client
    assert (await login(user, "operator")).status_code == 303
    await user.open("/admin")
    pick(user, "Пользователь", "bob")
    await user.should_see("Сохранить пользователя")
    user.find("Отображаемое имя", kind=ui.input).type("Борис")
    user.find("Лимит заявок", kind=ui.number).type("2")
    user.find("Лимит GPU", kind=ui.number).type("12")
    pick(user, "Роль", "admin")
    user.find("Разрешить вход", kind=ui.switch).click()
    user.find("Сохранить пользователя").click()
    await user.should_see("Настройки пользователя сохранены")
    account = store.user("bob")
    assert (account.role, account.display_name, account.run_limit, account.gpu_limit) == ("admin", "Борис", 2, 12)
    assert not account.enabled and not store.is_admin("bob")
    assert (await login(user, "bob")).status_code == 401
    for run_id, gpu in (("a", 10), ("b", 2), ("c", 1)):
        store.enqueue(BenchRun(id=run_id, status="queued", created_at=fakes.NOW.isoformat(), owner_id="bob"), gpu, run_id)
    assert not store.admit("a", Quotas())

    user.find("Разрешить вход", kind=ui.switch).click()
    user.find("Сохранить пользователя").click()
    assert store.admit("a", Quotas()) and store.admit("b", Quotas())
    assert not store.admit("c", Quotas(total_runs=3))
    with pytest.raises(ValueError, match="квоты"):
        store.grow_reservation("b", 3, Quotas())
    assert (await login(user, "bob")).status_code == 303
    assert (await client.get("/admin")).status_code == 200

    store.update_user("operator", "bob", role="user", enabled=True, display_name="Борис", run_limit=None, gpu_limit=None)
    store.register_user("bob", admin=True)  # auth.admins не перезаписывает роль из админки
    assert (await client.get("/admin")).status_code == 303  # прежний сеанс устарел
    assert (await login(user, "bob")).status_code == 303
    assert (await client.get("/admin")).status_code == 403


def test_active_request_keeps_original_quota_and_model_after_reassignment(tmp_path) -> None:
    store = Store(tmp_path / "db")
    try:
        for user_id in ("alice", "bob"):
            store.register_user(user_id)
        run = BenchRun(id="active", status="queued", owner_id="alice", created_at="now")
        assert store.enqueue(run, 8, "key")
        assert store.admit(run.id, Quotas())
        store.set_run_ownership("operator", run.id, "bob", [])
        assert store.get_bench(run.id).status == "running"
        assert store.get_run(run.id).owner_id == "bob"
        original = store.request_by_key("alice", "bench", "key")
        assert original["state"] == "reserved" and original["gpu"] == 8
    finally:
        store.close()
