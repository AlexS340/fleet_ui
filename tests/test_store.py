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


def test_bench_runs_and_stages(tmp_path: Path) -> None:
    from fleet_console.store import BenchRun

    store = Store(tmp_path / "c.sqlite")
    run = BenchRun(id="b1", status="running", created_at="2026-10-21T10:00:00+00:00", recipe="glm", jobs=2, agents_per_replica=5)
    store.create_bench(run)
    got = store.get_bench("b1")
    assert got and got.active and got.own_model and not got.holds_gpu and got.counters == {} and got.preview == {}
    store.update_bench("b1", version_id=7, counters_json='{"running": 3}', stage="execute")
    got = store.get_bench("b1")
    assert got.holds_gpu and got.counters == {"running": 3} and got.stage == "execute"
    sid = store.start_bench_stage("b1", "materialize", "uv run fleet …", "/tmp/m.log")
    assert store.open_bench_stage("b1").id == sid and store.open_bench_stage("b1").pid is None
    store.set_bench_stage_pid(sid, 4242)
    assert store.bench_stages("b1")[0].pid == 4242
    store.finish_bench_stage(sid, 0)
    assert store.open_bench_stage("b1") is None and store.bench_stages("b1")[0].returncode == 0
    store.create_bench(BenchRun(id="b2", status="done", created_at="2026-10-21T11:00:00+00:00", session_id="s1", version_id=9))
    assert [b.id for b in store.benches()] == ["b2", "b1"] and [b.id for b in store.benches(active_only=True)] == ["b1"]
    assert not store.get_bench("b2").own_model and not store.get_bench("b2").holds_gpu
    store.close()
