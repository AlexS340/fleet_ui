"""Квоты проверяются отдельными SQLite-соединениями, внешних сервисов нет."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from fleet_console.settings import Quotas
from fleet_console.store import BenchRun, Session, Store


def enqueue(store, name, owner="alice", gpu=8, session_id=None):
    run = BenchRun(id=name, owner_id=owner, status="queued", created_at="2026-11-11", session_id=session_id)
    assert store.enqueue(run, gpu, name)


@pytest.mark.parametrize("same_owner,expected", [(True, 1), (False, 2)])
def test_atomic_reservations_across_connections(tmp_path, same_owner, expected):
    path = tmp_path / "db"
    store = Store(path)
    quota = Quotas()
    for i in range(6):
        enqueue(store, str(i), "alice" if same_owner else f"user-{i}")
    barrier = Barrier(6)

    def claim(i):
        connection = Store(path)
        barrier.wait()
        try:
            return connection.admit(str(i), quota)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(claim, range(6))) == expected
    rows = store.conn.execute("SELECT * FROM requests WHERE state='reserved'").fetchall()
    assert sum(r["gpu"] for r in rows) == expected * 8
    store.close()


def test_repeated_submission_cancel_and_restart(tmp_path):
    path = tmp_path / "db"
    store = Store(path)
    enqueue(store, "first")
    enqueue(store, "second")
    assert store.admit("first", Quotas())
    assert not store.admit("second", Quotas())
    duplicate = BenchRun(id="third", owner_id="alice", status="queued", created_at="now")
    assert not store.enqueue(duplicate, 8, "first")
    assert store.get_bench("third") is None
    store.close()
    store = Store(path)
    assert store.request("first")["state"] == "reserved"
    assert not store.admit("second", Quotas())
    store.release("first")
    assert store.admit("second", Quotas())
    assert not store.admit("second", Quotas())
    store.close()


def test_session_and_bench_share_reservation(tmp_path):
    store = Store(tmp_path / "db")
    session = Session(id="model", owner_id="alice", recipe="qwen", status="queued", created_at="now")
    store.enqueue(session, 8, "model-key")
    assert store.admit("model", Quotas())
    store.update_session("model", status="ready")
    enqueue(store, "bench", gpu=0, session_id="model")
    enqueue(store, "another", gpu=0, session_id="model")
    assert store.admit("bench", Quotas())
    assert not store.admit("another", Quotas())
    store.update_session("model", status="stopped")
    store.release("model")
    assert store.request("model")["state"] == "reserved"
    store.release("bench")
    assert store.request("model")["state"] == "released"
    store.close()


def test_growth_and_unknown_legacy_gpu_are_conservative(tmp_path):
    store = Store(tmp_path / "db")
    enqueue(store, "a", gpu=4)
    assert store.admit("a", Quotas())
    store.grow_reservation("a", 8, Quotas())
    with pytest.raises(ValueError, match="квоты"):
        store.grow_reservation("a", 9, Quotas())
    store.grow_reservation("a", 1, Quotas())
    assert store.request("a")["gpu"] == 8  # запрошенное уменьшение ещё не подтверждено MLSC
    store.release("a")
    store.create_session(Session(id="old", recipe="qwen", status="attention", created_at="now"))
    enqueue(store, "b", owner="bob")
    assert not store.admit("b", Quotas())
    store.update_session("old", status="stopped")
    assert store.admit("b", Quotas())
    store.close()
