"""Гейт Ф5 на двух аккаунтах и локальной SQLite."""

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch

import fakes
import httpx
import pytest
from nicegui import core, ui
from nicegui.testing import User

from fleet_console.auth import Access, AccessDenied, Identity, IdentityMiddleware, socket_allowed
from fleet_console.auth import TestIdentityAdapter as LoginAdapter
from fleet_console.compose import BenchSpec
from fleet_console.presets import load_presets, personal_dir
from fleet_console.runner import Result
from fleet_console.settings import OVERLAY_DIR
from fleet_console.ui.measurements import issue_link, revoke_link

pytestmark = pytest.mark.nicegui_main_file("tests/auth_ui_main.py")


async def login(client: httpx.AsyncClient, user: str, password: str | None = None) -> httpx.Response:
    return await client.post("/test-login", data={"user": user, "password": password or f"{user}-test"})


async def test_logout_revokes_only_current_browser_session(user: User, monkeypatch) -> None:
    client = user.http_client
    assert (await login(client, "alice")).status_code == 303
    old_cookie = client.cookies[LoginAdapter.cookie]
    page = await user.open("/measurements")
    monkeypatch.setattr(core.sio, "get_environ", lambda _: {"asgi.scope": page.request.scope})
    assert socket_allowed("alice", page.id, joined=False)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(core.app), base_url="http://test") as other:
        assert (await login(other, "alice")).status_code == 303
        assert (await client.get("/test-logout")).status_code == 405
        assert (await client.post("/test-logout")).status_code == 403
        assert (await client.get("/measurements")).status_code == 200
        assert (await client.post("/test-logout", headers={"X-Fleet-Logout": "1"})).status_code == 204
        assert LoginAdapter.cookie not in client.cookies
        assert not socket_allowed("alice", page.id, joined=False)
        response = await client.get("/measurements")
        assert response.status_code == 303 and response.headers["location"] == "/test-login"

        client.cookies.set(LoginAdapter.cookie, old_cookie, domain="test.local", path="/")
        assert (await client.get("/measurements")).status_code == 303
        assert (await other.get("/measurements")).status_code == 200
        assert (await login(client, "bob")).status_code == 303
        await user.open("/measurements")
        await user.should_see("bob-cowork")
        await user.should_not_see("alice-cowork")


async def test_two_accounts_and_shared_summary(user: User) -> None:
    ctx = fakes.CTX
    client = user.http_client
    now = datetime.now(UTC)
    assert (await login(client, "alice", "bad-password")).status_code == 401
    assert (await client.get("/runs/alice-cowork")).status_code == 303

    assert (await login(client, "alice")).status_code == 303
    await user.open("/measurements")
    await user.should_see("alice-cowork")
    await user.should_not_see("bob-cowork")
    await user.open("/runs")
    assert ctx.store.get_bench("alice-cowork").owner_id == "alice"
    assert ctx.store.draft_owner("alice-draft") == "alice"
    table = next(t for t in user.find(kind=ui.table).elements if t.row_key == "id" and t.columns[0]["name"] == "id")
    assert [r["id"] for r in table.rows] == ["alice-cowork"]
    await user.should_see(marker="local-datasets")
    local = next(iter(user.find(marker="local-datasets").elements))
    assert [row["id"] for row in local.rows] == ["local-alice"]
    assert (await client.get("/runs/bob-cowork")).status_code == 404
    assert (await client.get("/settings")).status_code == 403

    alice = Access(ctx.store, Identity("alice"))
    await user.open("/measurements")
    user.find("alice-cowork", kind=ui.expansion).click()
    user.find("Поделиться", kind=ui.button).click()
    await user.should_see("Ссылок пока нет")
    user.find("Пометка для себя", kind=ui.input).type("Для команды")
    user.find("Создать ссылку").click()
    await user.should_see("Для команды")
    url = next(iter(user.find("Ссылка для отправки", kind=ui.input).elements)).value
    token = url.rsplit("/", 1)[-1]
    expired = issue_link(alice, "alice-cowork", 1, now - timedelta(hours=2))
    shared = f"/measurements/shared/{token}"
    client.cookies.clear()
    response = await client.get(shared)
    assert response.status_code == 303 and "next=" in response.headers["location"]

    signed_in = await client.post(response.headers["location"], data={"user": "bob", "password": "bob-test"})
    assert signed_in.status_code == 303 and signed_in.headers["location"] == shared
    await user.open("/measurements")
    await user.should_see("bob-cowork")
    await user.should_not_see("alice-cowork")
    await user.open("/models")
    assert [r["id"] for r in next(iter(user.find(kind=ui.table).elements)).rows] == ["bob-model"]
    assert (await client.get("/runs/alice-cowork")).status_code == 404
    assert (await client.get("/models/alice-model")).status_code == 404
    await user.open(shared)
    await user.should_see("Сводка замера")
    await user.should_not_see("Открыть прогон")
    await user.should_not_see("Поделиться")
    await user.should_not_see("Для команды")
    rows = next(iter(user.find(kind=ui.table).elements)).rows
    assert rows[0]["score"] == "75.00%" and rows[0]["succeeded"] == "3/4" and rows[0]["infra_errors"] == 1
    assert (await client.get(shared)).headers["cache-control"] == "no-store"
    assert (await client.get(f"/measurements/shared/{expired}")).status_code == 404

    digest = next(row["digest"] for row in ctx.store.measurement_links("alice-cowork") if row["expires_at"] > now.isoformat())
    bob = Access(ctx.store, Identity("bob"))
    with pytest.raises(AccessDenied):
        revoke_link(bob, "alice-cowork", digest)
    await login(client, "alice")
    await user.open("/measurements")
    user.find("alice-cowork", kind=ui.expansion).click()
    user.find("Поделиться", kind=ui.button).click()
    await user.should_see("Для команды")
    await user.should_see("Истекла")
    user.find("Отозвать").click()
    await user.should_see("Отозвана")
    await login(client, "bob")
    assert (await client.get(shared)).status_code == 404


async def test_coauthor_cannot_manage_measurement_links(user: User) -> None:
    store = fakes.CTX.store
    run_id = "alice-cowork"
    owner = Access(store, Identity("alice"))
    now = datetime.now(UTC)
    issue_link(owner, run_id, 1, now)
    digest = store.measurement_links(run_id)[0]["digest"]
    store.register_user("cara")
    store.set_run_ownership("operator", run_id, "alice", ["cara"])
    access = Access(store, Identity("cara"))
    assert access.run(run_id)
    with pytest.raises(AccessDenied):
        issue_link(access, run_id, 1, now)
    with pytest.raises(AccessDenied):
        revoke_link(access, run_id, digest)

    # Открытая у прежнего владельца форма также теряет право выдачи и отзыва.
    await login(user.http_client, "alice")
    await user.open("/measurements")
    user.find(run_id, kind=ui.expansion).click()
    user.find("Поделиться", kind=ui.button).click()
    await user.should_see("Выданные ссылки")
    store.set_run_ownership("operator", run_id, "cara", ["alice"])
    user.find("Создать ссылку").click()
    await user.should_see("Объект недоступен")
    assert len(store.measurement_links(run_id)) == 1
    await user.open("/measurements")
    user.find(run_id, kind=ui.expansion).click()
    await user.should_see("Открыть прогон")
    await user.should_not_see("Поделиться")


async def test_user_can_prepare_revision_for_own_launches(user: User) -> None:
    ctx = fakes.CTX
    client = user.http_client
    await ctx.manager.select("bob", "master")
    assert (await login(client, "alice")).status_code == 303
    await user.open("/")
    await user.should_not_see("Актуализировать")
    branch = next(e for e in user.find(kind=ui.select).elements if e.props.get("label") == "Ветка")
    branch.set_value("feat/x")
    await user.should_see("отстаёт от master на 3")
    assert not ctx.manager.is_ready(fakes.SHA_FEAT)

    user.find("Подготовить ревизию").click()
    await user.should_see("Готово: " + fakes.SHA_FEAT[:8])
    assert ctx.manager.selected("alice").info.sha == fakes.SHA_FEAT
    assert ctx.manager.selected("bob").info.sha == fakes.SHA_MASTER

    await user.open("/runs")
    user.find("Запустить бенч").click()
    await user.should_see("ревизия " + fakes.SHA_FEAT[:8])
    await user.open("/models")
    user.find("Поднять модель").click()
    await user.should_see("ревизия " + fakes.SHA_FEAT[:8])


async def test_owner_can_reuse_run_but_personal_preset_is_not_listed_for_other_user(user: User) -> None:
    ctx = fakes.CTX
    revision = await ctx.manager.prepare("master")
    BenchSpec(
        run_id="alice-cowork", worktree=revision.worktree, run_dir=ctx.paths.runs / "alice-cowork", overlay=OVERLAY_DIR,
        recipe="glm", agent="terminus2", dataset="cowork", sha=revision.info.sha,
    ).write()
    client = user.http_client
    assert (await login(client, "alice")).status_code == 303
    await user.open("/runs/alice-cowork")
    user.find("Сохранить как пресет").click()
    user.find(marker="save-personal-preset").click()
    await user.should_see("Пресет «alice-cowork» сохранён")
    presets, errors = load_presets(personal_dir(ctx.paths.home, "alice"))
    assert not errors and [p.name for p in presets] == ["alice-cowork"]

    user.find("Запустить с теми же параметрами").click()
    await user.should_see("Новый прогон из истории")
    assert len(ctx.store.benches()) == 2

    assert (await login(client, "bob")).status_code == 303
    assert (await client.get("/runs/alice-cowork")).status_code == 404
    await user.open("/runs")
    user.find("Запустить бенч").click()
    preset = next(e for e in user.find(kind=ui.select).elements if e.props.get("label") == "Пресет")
    assert not any("alice-cowork" in key for key in preset.options)


async def test_foreign_resources_denied_before_external_calls(user: User) -> None:
    ctx = fakes.CTX
    bob = Access(ctx.store, Identity("bob"))
    calls = [
        (ctx.benches.stop, ("alice-cowork",), {}),
        (ctx.benches.retry, ("alice-cowork",), {}),
        (ctx.benches.measure, ("alice-cowork",), {}),
        (ctx.benches.model_log, ("alice-cowork",), {}),
        (ctx.benches.s3_objects, ("alice-cowork",), {}),
        (ctx.benches.s3_text, ("s3://fake/runs/alice-cowork/logs",), {"run_id": "bob-cowork"}),
        (ctx.benches.task_log, ("alice-workflow",), {"run_id": "alice-cowork"}),
        (ctx.sessions.stop, ("alice-model",), {}),
        (ctx.sessions.extend, ("alice-model", 60), {}),
        (ctx.sessions.set_replicas, ("alice-model", 0), {}),
    ]
    for method, args, kwargs in calls:
        with pytest.raises(AccessDenied):
            await method(*args, **kwargs, access=bob)
    with pytest.raises(AccessDenied):
        bob.draft("alice-draft")
    with pytest.raises(AccessDenied):
        bob.require(ctx.local_datasets.get("local-alice"))
    ctx.store.reserve_draft("bob-draft", "bob")
    spec = SimpleNamespace(run_id="bob-draft", session=SimpleNamespace(id="alice-model"))
    with pytest.raises(AccessDenied):
        await ctx.benches.start(spec, ref="master", preview=None, access=bob)


async def test_measurement_records_reward_and_resolved_count_separately(user: User) -> None:
    ctx = fakes.CTX
    run = ctx.store.get_bench("alice-cowork")
    log = ctx.paths.runs / run.id / "logs" / "measure.log"
    log.parent.mkdir(parents=True)
    log.write_text("cowork/terminus-2: 62.50% (2.5/4, failed=1, timeout=0, no_attempt=0)\n")
    async def summary(argv, **kwargs):
        return Result(tuple(argv), 0, '{"run_id":"alice-cowork","resolved_tasks":2}')
    with patch.object(ctx.runner, "run", summary):
        await ctx.benches.save_measurement(run.id)
    saved = ctx.store.measurement(run.id)
    results = json.loads(saved["results_json"])
    assert saved["sha"] == fakes.SHA_MASTER
    assert results[0]["score"] == 62.5 and results[0]["succeeded"] == 2 and results[0]["infra_errors"] == 1


async def test_missing_infrastructure_adapter_fails_closed(user: User) -> None:
    async def protected(scope, receive, send):
        pytest.fail("защищённый маршрут не должен вызываться")

    app = IdentityMiddleware(protected, adapter=None, store=fakes.CTX.store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get("/measurements", headers={"X-User": "alice"})
    assert response.status_code == 401


async def test_socket_guard_requires_matching_identity_and_room(user: User, monkeypatch) -> None:
    from fleet_console.auth import IDENTITY

    await login(user.http_client, "alice")
    page = await user.open("/measurements")
    monkeypatch.setattr(core.sio, "get_environ", lambda sid: {"asgi.scope": {IDENTITY: Identity(sid)}})
    monkeypatch.setattr(core.sio, "rooms", lambda sid: [page.id] if sid == "alice" else [])
    assert socket_allowed("alice", page.id, joined=True)
    assert not socket_allowed("bob", page.id, joined=False)
    monkeypatch.setattr(core.sio, "rooms", lambda sid: [])
    assert not socket_allowed("alice", page.id, joined=True)
