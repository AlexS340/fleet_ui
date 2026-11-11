import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from fleet_console.runs.private_measure import PrivateMeasureRunner


def test_adapter_uses_read_only_and_preserves_fractional_reward(tmp_path, monkeypatch):
    calls = []
    module = ModuleType("benchboard.measure")

    def measure(uri, **kwargs):
        calls.append((uri, kwargs))
        assert kwargs["read_only"] is True
        return [SimpleNamespace(summary=SimpleNamespace(
            benchmark="birdsql", framework="terminus-2", score=50, planned_tasks=3, resolved_tasks=1,
            reward_sum=1.5, error_tasks=1, timeout_tasks=0, no_attempt_tasks=0,
        ))]

    module.measure_run = measure
    monkeypatch.setitem(sys.modules, "benchboard.measure", module)
    output = tmp_path / "measurement.json"
    args = dict(run_uri="s3://private/results/model/id", run_id="id", sha="a" * 40, output_file=str(output))
    PrivateMeasureRunner(**args).run()
    data = json.loads(output.read_text())
    assert data["run_id"] == "id" and data["sha"] == "a" * 40
    assert data["results"][0]["succeeded"] == 1 and data["results"][0]["reward_sum"] == 1.5
    assert len(calls) == 1 and not output.with_suffix(".tmp").exists()
    with pytest.raises(ValueError):
        PrivateMeasureRunner(**args, read_only=False)


def test_adapter_failure_does_not_publish_partial_summary(tmp_path, monkeypatch):
    module = ModuleType("benchboard.measure")

    def fail(*args, **kwargs):
        raise RuntimeError("S3 unavailable")

    module.measure_run = fail
    monkeypatch.setitem(sys.modules, "benchboard.measure", module)
    output = tmp_path / "measurement.json"
    with pytest.raises(RuntimeError, match="S3 unavailable"):
        PrivateMeasureRunner(run_uri="s3://private/result", run_id="id", sha="a", output_file=str(output)).run()
    assert not output.exists()
