from pathlib import Path

from fleet_console.store import Store


def test_events_newest_first(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    store.event("fetch", "fleet.git", "ok")
    store.event("worktree", "abc", "error", "нет места")
    events = store.events()
    assert [e.action for e in events] == ["worktree", "fetch"]
    assert events[0].details == "нет места"
    store.close()


def test_revisions_crud(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    assert store.get_revision("abc") is None
    store.upsert_revision("abc", {"harbor": "0.22.0", "steps": ["mlsc.create_job"]}, ref="master")
    rev = store.get_revision("abc")
    assert rev and rev.profile["steps"] == ["mlsc.create_job"] and rev.ref == "master"
    store.upsert_revision("abc", {"harbor": "0.23.0"})
    assert store.get_revision("abc").profile == {"harbor": "0.23.0"}
    store.touch_revision("abc")
    assert [r.sha for r in store.revisions()] == ["abc"]
    store.delete_revision("abc")
    assert store.revisions() == []
    store.close()


def test_own_runs_adopt_and_forget(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    assert store.get_run("run-a") is None and store.own_runs() == {}
    store.adopt_run("run-a", 41, model="GLM", note="ручной smoke")
    run = store.get_run("run-a")
    assert run and run.version_id == 41 and run.note == "ручной smoke" and run.adopted_at
    assert run.owns_version(41, "whatever") and run.owns_version(7, "gigacode-fleet-egbulychev run-a")
    assert not run.owns_version(7, "someone else")
    store.adopt_run("run-a", None)  # повтор — обновление, не ошибка
    assert store.get_run("run-a").version_id is None
    assert list(store.own_runs()) == ["run-a"]
    store.forget_run("run-a")
    assert store.own_runs() == {}
    store.close()
