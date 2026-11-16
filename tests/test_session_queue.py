import asyncio

import pytest

from test_sessions import Harness
from fleet_console.auth import Access, Identity
from fleet_console.models import SessionError
from fleet_console.settings import Quotas


async def test_model_queue_and_confirmation_key(tmp_path):
    h = Harness(tmp_path)
    h.mgr.settings.quotas = Quotas()
    alice = Access(h.store, Identity("alice"))
    args = dict(recipe="glm", replicas=1, access=alice, request_key="one")
    a, same = await asyncio.gather(h.mgr.start(h.revision, **args), h.mgr.start(h.revision, **args))
    assert a.id == same.id and len(h.mgr.sessions()) == 1
    await h.mgr.tasks[a.id]
    b = await h.mgr.start(h.revision, **{**args, "request_key": "two"})
    assert b.status == "queued" and b.id not in h.mgr.tasks
    assert h.steps.count("create_job") == 1
    with pytest.raises(SessionError, match="квоту"):
        await h.mgr.set_replicas(a.id, 2, access=alice)
    assert h.steps.count("start_llm") == 1
    assert (await h.mgr.stop(a.id, access=alice)).status == "stopped"
    await h.mgr.tick()
    await h.mgr.tasks[b.id]
    assert h.mgr.get(b.id).status == "ready" and h.steps.count("create_job") == 2
    assert a.model != b.model
    assert h.store.request(b.id)["gpu"] == 8
    await h.mgr.stop(b.id, access=alice)


async def test_cancel_waiting_session_has_no_external_effects(tmp_path):
    h = Harness(tmp_path)
    h.mgr.settings.quotas = Quotas()
    a = await h.mgr.start(h.revision, recipe="glm")
    await h.mgr.tasks[a.id]
    b = await h.mgr.start(h.revision, recipe="glm")
    calls = list(h.runner.calls)
    await h.mgr.stop(b.id)
    assert calls == h.runner.calls and h.mgr.get(b.id).status == "stopped"
    await h.mgr.stop(a.id)
