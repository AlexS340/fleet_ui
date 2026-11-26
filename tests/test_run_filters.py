from datetime import datetime, timedelta, timezone
from itertools import combinations

import fakes
import pytest
from nicegui import ui
from nicegui.testing import User

from fleet_console.store import BenchRun
from fleet_console.ui.benches import _matches_run_filters


def test_hidden_status_combinations() -> None:
    statuses = ("failed", "stopped", "running")
    for size in range(4):
        for hidden in combinations(statuses, size):
            for status in (*statuses, "done", "attention", "queued", "stopping"):
                run = BenchRun("id", status, "2026-01-01")
                assert _matches_run_filters(run, list(hidden), None, "", "") == (status not in hidden)
            run = BenchRun("id", "done", "2026-01-01", failed_attempts=2)
            assert _matches_run_filters(run, list(hidden), None, "", "") == ("failed" not in hidden)


@pytest.mark.nicegui_main_file("tests/auth_ui_main.py")
async def test_filters_use_visible_history_before_row_limit(user: User) -> None:
    store = fakes.CTX.store
    await user.http_client.post("/test-login", data={"user": "alice", "password": "alice-test"})
    # Alice — соавтор одного старого прогона Bob. Ещё один его прогон и все прогоны Carol закрыты.
    start = datetime(2026, 2, 1, 13, 55, 59, tzinfo=timezone(timedelta(hours=5)))
    minute = start.astimezone().strftime("%Y-%m-%dT%H:%M")
    for key, status, owner, offset in (
        ("shared", "done", "bob", 0), ("private", "done", "carol", 0), ("later", "failed", "alice", 1),
    ):
        stamp = (start + timedelta(minutes=offset)).isoformat()
        store.create_bench(BenchRun(key, status, stamp, started_at=stamp, owner_id=owner))
    store.set_run_ownership("operator", "shared", "bob", ["alice"])
    for index in range(51):
        store.create_bench(BenchRun(f"pending-{index}", "queued", fakes.NOW.isoformat(), owner_id="alice"))

    await user.open("/runs")
    table = user.find(marker="bench-runs").elements.pop()
    assert len(table.rows) == 50 and "shared" not in {r["id"] for r in table.rows}
    user.find("Фильтры").click()
    selects = {s.props["label"]: s for s in user.find(kind=ui.select).elements}
    owner, hidden = selects["Владелец"], selects["Скрыть статусы"]
    dates = {i.props["label"]: i for i in user.find(kind=ui.input).elements}
    assert set(owner.options) == {"alice", "bob"}

    dates["Начат с"].set_value(minute)
    assert {r["id"] for r in table.rows} == {"shared", "later"}
    dates["Начат по"].set_value(minute)
    assert [r["id"] for r in table.rows] == ["shared"]
    dates["Начат с"].set_value("")
    assert [r["id"] for r in table.rows] == ["shared"]
    owner.set_value("alice")
    await user.should_see("По этим фильтрам прогонов нет")
    owner.set_value("bob")
    hidden.set_value(["failed", "running", "stopped"])
    assert [r["id"] for r in table.rows] == ["shared"]

    dates["Начат с"].set_value("2027-01-01T00:00")
    await user.should_see("Дата «Начат с» позже «Начат по»")
    user.find("Сбросить").click()
    assert len(table.rows) == 50 and not any((hidden.value, owner.value, dates["Начат с"].value, dates["Начат по"].value))
    owner.set_value("bob")
    hidden.set_value(["queued"])
    store.set_run_ownership("operator", "shared", "bob", [])
    await user.should_see("Фильтры · найдено 2", retries=45)
    assert set(owner.options) == {"alice"}
    assert {r["id"] for r in table.rows} == {"alice-cowork", "later"}


@pytest.mark.nicegui_main_file("tests/ui_main.py")
async def test_banner_summarizes_run_errors_and_hides_legacy_mlsc_payload(user: User) -> None:
    store = fakes.CTX.store
    legacy = ("Не удалось сверить остановку: kubectl exec -i -n ns pod -- sh -c "
              "'curl https://mlsc/api/v1/service/get_configuration' → код -2\n"
              '{"applications":[{"name":"PRIVATE"}]}')
    for run_id, status, error in (
        ("stop-test", "stopping", legacy),
        ("attention-test", "attention", "Ошибка стадии\n" + "x" * 400),
    ):
        store.create_bench(BenchRun(run_id, status, fakes.NOW.isoformat(), error=error))
    await user.open("/runs")
    banner = user.find("⚠ stop-test").elements.pop().text
    assert "Не удалось сверить остановку: ошибка запроса MLSC (код -2)" in banner
    assert "Ошибка стадии x" in banner and "…" in banner
    assert "PRIVATE" not in banner and "kubectl" not in banner and "x" * 200 not in banner
    errors = {row["id"]: row["errors"] for row in user.find(marker="bench-runs").elements.pop().rows}
    assert errors["stop-test"] == ["Не удалось сверить остановку: ошибка запроса MLSC (код -2)"]
    assert len(errors["attention-test"][0]) == 200
