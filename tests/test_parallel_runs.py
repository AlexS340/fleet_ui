"""Гейт Ф6: два аккаунта, повтор подтверждения, очередь, обрывы — FakeRunner, без GPU."""

import asyncio
import json

import pytest

from test_benches import Harness, T0
from fleet_console.auth import Access, AccessDenied, Identity
from fleet_console.compose import Preview, make_bench_id
from fleet_console.compose.policy import private_root
from fleet_console.observe.mlsc import created_version
from fleet_console.settings import Quotas, Settings


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    harness.settings.quotas = Quotas()
    harness.settings.auth.mode = "test"
    harness.settings.results.private_s3_root = "s3://console-private/results"
    harness.settings.bench.requeue_rounds = 0
    return harness


def draft(h, owner):
    spec = h.spec(run_id=make_bench_id("same-preset", T0))
    h.store.reserve_draft(spec.run_id, owner, spec.sha)
    return spec, Access(h.store, Identity(owner))


async def submit(h, spec, access):
    return await h.mgr.start(spec, ref="master", preview=Preview(), access=access)


async def test_two_accounts_concurrent_and_stop_isolated(h):
    specs = [draft(h, owner) for owner in ("alice", "bob")]
    waiting = set()
    gate = asyncio.Event()

    async def wait(_):
        waiting.add(asyncio.current_task().get_name())
        if len(waiting) == 2:
            gate.set()
        await asyncio.Event().wait()

    h.mgr.sleep = wait
    first, second = await asyncio.gather(*(submit(h, *args) for args in specs))
    try:
        await asyncio.wait_for(gate.wait(), 3)
        again = await submit(h, *specs[0])
        assert again.id == first.id and len(h.store.benches()) == 2
        assert first.id != second.id and first.model != second.model
        assert first.preview["s3"] != second.preview["s3"]
        assert first.sha == second.sha == specs[0][0].sha
        assert h.stages.count("create_job") == 2
        with pytest.raises(AccessDenied):
            await h.mgr.stop(first.id, access=specs[1][1])
        stopped = await h.mgr.stop(first.id, access=specs[0][1])
        assert stopped.status == "stopped"
        assert h.stopped == [first.id]
        assert h.mgr.get(second.id).status == "running"
        assert h.store.request(second.id)["state"] == "reserved"
        assert not h.mgr.tasks[second.id].done()
    finally:
        await h.mgr.stop(second.id, access=specs[1][1])


async def test_queue_duplicate_and_cancel_without_cluster(h):
    # Не исполняем стадии: проверяем, что очередь не обращается к внешним операциям.
    launched = []
    h.mgr._launch = lambda *args, **kw: launched.append(args[0])
    a, b, c = [draft(h, owner) for owner in ("alice", "alice", "bob")]
    one, two = await asyncio.gather(submit(h, *a), submit(h, *b))
    assert one.status == "running" and two.status == "queued"
    three = await submit(h, *c)
    assert three.status == "running" and launched == [one.id, three.id]
    before = list(h.runner.calls)
    await h.mgr.stop(two.id, access=b[1])
    assert h.runner.calls == before and not h.stopped
    assert h.store.request(two.id)["state"] == "released"
    assert (await submit(h, *b)).status == "stopped"  # повтор подтверждения не воскресил отменённую заявку


async def test_queue_drains_after_release_and_survives_resume(h):
    launched = []
    h.mgr._launch = lambda *args, **kw: launched.append(args[0])
    a, b = draft(h, "alice"), draft(h, "alice")
    one, two = await submit(h, *a), await submit(h, *b)
    await h.mgr.resume()
    assert two.id not in launched
    h.store.update_bench(one.id, status="stopped")
    h.store.release(one.id)
    await h.mgr.tick()
    assert launched.count(two.id) == 1 and h.mgr.get(two.id).status == "running"


async def test_unknown_submitter_never_relaunches(h):
    spec, access = draft(h, "alice")
    h.submitter = "done"
    h.fail = {"upload"}
    run = await submit(h, spec, access)
    await h.mgr.tasks[run.id]
    h.store.update_bench(run.id, stage="launch")
    h.store.start_bench_stage(run.id, "launch", "interrupted")
    h.fail.clear()
    before = h.stages.count("launch")
    await h.mgr.retry(run.id, access=access)
    await h.mgr.tasks[run.id]
    assert h.mgr.get(run.id).status == "attention"
    assert h.stages.count("launch") == before
    assert h.store.request(run.id)["state"] == "reserved"


async def test_create_job_recovers_by_exact_run_id_without_duplicate(h):
    spec, access = draft(h, "alice")
    h.fail = {"check"}
    run = await submit(h, spec, access)
    await h.mgr.tasks[run.id]
    original = h.mgr.get(run.id).version_id
    h.store.update_bench(run.id, stage="create_job", version_id=None)
    spec.version_id_file.unlink()
    h.fail.clear()
    h.submitter = "done"
    await h.mgr.retry(run.id, access=access)
    await h.mgr.tasks[run.id]
    assert h.mgr.get(run.id).status == "done"
    assert h.mgr.get(run.id).version_id == original
    assert h.stages.count("create_job") == 1
    assert h.store.request(run.id)["state"] == "released"
    result = json.loads(h.store.measurement(run.id)["results_json"])[0]
    assert result["reward_sum"] == 1.5 and result["succeeded"] == 1


async def test_stop_exit_zero_does_not_release_live_gpu(h):
    spec, access = draft(h, "alice")
    h.fail = {"check"}
    run = await submit(h, spec, access)
    await h.mgr.tasks[run.id]
    h.still_running = True
    assert (await h.mgr.stop(run.id, access=access)).status == "stopping"
    assert h.store.request(run.id)["state"] == "reserved"
    h.still_running = False
    assert (await h.mgr.stop(run.id, access=access)).status == "stopped"
    assert h.store.request(run.id)["state"] == "released"


def test_private_storage_and_exact_reconciliation():
    settings = Settings()
    settings.auth.mode = "shared"
    with pytest.raises(ValueError, match="закрытый S3"):
        private_root(settings)
    settings.results.private_s3_root = "s3://private/results"
    assert private_root(settings) == "s3://private/results"
    configuration = {"application_versions": {"7": {"application_name": "user | fleet-console run-a"},
                                               "8": {"application_name": "user | fleet-console run-ab"}}}
    assert created_version(configuration, "run-a") == 7
    with pytest.raises(RuntimeError):
        created_version(configuration, "run", 7)
    configuration["application_versions"]["9"] = {"application_name": "other | fleet-console run-a"}
    with pytest.raises(RuntimeError):
        created_version(configuration, "run-a")


async def test_two_accounts_keep_separate_completed_measurements(h):
    h.submitter = "done"
    runs = await asyncio.gather(*(submit(h, *draft(h, owner)) for owner in ("alice", "bob")))
    await asyncio.gather(*(h.mgr.tasks[run.id] for run in runs))
    for owner, run in zip(("alice", "bob"), runs):
        assert h.mgr.get(run.id).status == "done"
        rows = h.store.measurements(owner)
        assert len(rows) == 1 and rows[0]["run_id"] == run.id
        assert h.store.request(run.id)["state"] == "released"
