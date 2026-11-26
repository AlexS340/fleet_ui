import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from cluster_samples import completed_workflow

from fleet_console.observe.argo_history import DAY, ArgoHistory
from fleet_console.runner import FakeRunner, Result
from fleet_console.store import Store


async def test_watch_pagination_replay_restart_and_expiry(tmp_path):
    store = Store(tmp_path / "db")
    runner = FakeRunner()
    history = ArgoHistory(store.conn, runner, dict)
    requests = []
    api = ("kubectl", "--context", "internal", "get", "--raw")
    scope = "internal/ns"
    current = completed_workflow()
    replies = iter([
        {"metadata": {"resourceVersion": "20", "continue": "page/2"}, "items": [completed_workflow("old", age=DAY + 5)]},
        {"metadata": {"resourceVersion": "20"}, "items": [completed_workflow("success", age=4000, phase="Succeeded")]},
        '\n'.join(json.dumps({"type": kind, "object": current}) for kind in ("ADDED", "MODIFIED", "DELETED")),
        {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "25"}}},
        {"type": "ERROR", "object": {"code": 410, "reason": "Expired"}},
        {"metadata": {"resourceVersion": "40"}, "items": []},
    ])

    def answer(argv):
        requests.append(parse_qs(urlsplit(argv[-1]).query))
        data = next(replies)
        return Result(argv, 0, data if isinstance(data, str) else json.dumps(data))

    runner.on(*api, result=answer)
    await history.collect("internal", "ns")
    assert requests[1]["continue"] == ["page/2"]
    await history.collect("internal", "ns")
    assert requests[2]["resourceVersion"] == ["20"]
    store.close()
    store = Store(tmp_path / "db")
    history = ArgoHistory(store.conn, runner, dict)
    await history.collect("internal", "ns")
    assert requests[3]["resourceVersion"] == ["22"]
    with pytest.raises(RuntimeError, match="Expired"):
        await history.collect("internal", "ns")
    await history.collect("internal", "ns")
    assert "watch" not in requests[-1] and history.checkpoint(scope)["rv"] == "40"
    now = int(time.time())
    assert dict(history.groups(scope, now - DAY)[0]) == {
        "run": "run-a", "failed": 1, "succeeded": 1, "last": history.find(scope, now - 3600, "wf-x")["finished"],
    }
    assert [r["name"] for r in history.workflows(scope, now - 3600, "run-a", 0)] == ["wf-x"]
    assert not history.find(scope, 0, "old") and not history.groups("other/ns", 0)
    assert "not-for-sqlite" not in history.find(scope, 0, "wf-x")["node_error"]
    store.close()


async def test_pipeline_uses_attempt_for_alert_pod_and_caches(tmp_path):
    runner = FakeRunner()
    store = Store(tmp_path / "db")
    history = ArgoHistory(store.conn, runner, lambda: {"S": "private-value"})
    history.save("c/n", [completed_workflow()], "12")
    prefix = "run-a/bench/agent/results/task/"
    runner.on("aws", "s3api", stdout=json.dumps([prefix + "try1/", prefix + "try2/", prefix + "noise/"]))

    def cat(argv):
        if "node_info.json" in argv[-1]:
            pod = "wf-other-pod" if "try1/" in argv[-1] else "wf-x-runner-123"
            return Result(argv, 0, json.dumps({"pod_name": pod}))
        assert "try2/pipeline.log" in argv[-1]
        return Result(argv, 0, "[fail] pull_task_image private-value\n[fail] secondary error")

    runner.on("sh", "-c", result=cat)
    row = history.find("c/n", 0, "wf-x")
    assert await history.pipeline_error(row, {}) == "[fail] pull_task_image <secret>"
    count = len(runner.calls)
    assert await history.pipeline_error(history.find("c/n", 0, "wf-x"), {}) == "[fail] pull_task_image <secret>"
    assert len(runner.calls) == count
    store.close()
