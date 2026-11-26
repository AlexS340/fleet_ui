import json

import pytest

from fleet_console.compose.task_resources import compare, task_resources
from fleet_console.connect.kube import KubeStatus
from fleet_console.runner import FakeRunner


def report():
    return {
        "selector": {"workload": "cpu", "zone": "east"},
        "tolerations": [{"key": "type", "value": "bench"}],
        "runner": ["250m", "512Mi"],
        "tasks": {"tasks/small": [".75", "0.5Gi"], "tasks/large": ["3750m", "1536Mi"]},
    }


def test_each_task_must_fit_on_one_eligible_node():
    nodes = (
        'True\t\t[{"key":"type","value":"bench","effect":"NoSchedule"}]\t4000m\t1024Mi\n'
        "True\tfalse\t\t1e0\t4Gi\n"
        'True\t\t[{"key":"gpu","effect":"NoExecute"}]\t32\t64Gi\n'
        "False\t\t\t32\t64Gi\nTrue\ttrue\t\t32\t64Gi\n"
    )
    check = compare(report(), nodes, "bench")
    assert check.level == "warn" and "large (4 CPU, 2048 MiB)" in check.detail
    assert "small" not in check.detail and "узлов: 2" in check.detail
    exact = compare(report(), nodes + "True\t\t\t4\t2097152Ki\n", "bench")
    assert exact.ok


@pytest.mark.parametrize("nodes,detail", [
    ("", "нет готовых узлов"), ("True\t\t\tbad\t4Gi\n", "неполная"),
    ("True\t\t\t1\tunknown\n", "large (4 CPU, 2048 MiB)"),
])
def test_missing_capacity_is_not_assumed_zero(nodes, detail):
    check = compare(report(), nodes, "bench")
    assert check.level == "warn" and detail in check.detail


async def test_resolved_builder_and_filtered_pool_query(tmp_path):
    runner = FakeRunner()
    kube = KubeStatus(context="approved", ready=True, allowed=True)
    builder = {"_target_": "fleet.HarborWorkflowBuilder", "runner_cpu_request": "250m"}
    source = {"_target_": "fleet.HarborTaskMaterializer", "job_source": {"config": {"tasks": [{"path": "tasks/small"}]}}}
    resolved = {"system": {"materialize": source, "workflow_builder": {},
                           "manifest_runner": {"_target_": "fleet.BatchWorkflowRunner", "workflow_builder": builder}}}
    runner.on(str(tmp_path / ".venv/bin/python"), stdout=json.dumps(report()))
    args = ("kubectl", "--context", "approved", "get", "nodes", "-l", "workload=cpu,zone=east")
    runner.on(*args, stdout="True\t\t\t4\t2Gi\n")
    check = await task_resources(runner, tmp_path, resolved, kube)
    assert check.ok and "workload=cpu,zone=east" in check.detail
    assert json.loads(runner.calls[0][1]["stdin"]) == {"config": builder, "materialize": source}
    assert runner.calls[0][1]["cwd"] == tmp_path
    runner.handlers.pop()
    assert "Не удалось прочитать" in (await task_resources(runner, tmp_path, resolved, kube)).detail
    runner.calls.clear()
    assert (await task_resources(runner, tmp_path, resolved, KubeStatus())).level == "warn"
    assert runner.calls == []


async def test_probe_uses_harbor_selection_and_revision_builder(tmp_path, monkeypatch):
    import runpy
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from fleet_console.compose import task_resources as resources

    paths = [tmp_path / name for name in ("selected", "extra")]
    selected = [SimpleNamespace(path=p) for p in paths]
    dataset = Mock()
    dataset.get_task_configs = AsyncMock(return_value=selected[:1])
    jobs = [SimpleNamespace(tasks=[], datasets=[dataset], verifier=SimpleNamespace(disable=True)),
            SimpleNamespace(tasks=selected, datasets=[], verifier=SimpleNamespace(disable=False))]
    source = Mock()
    source.model_validate.return_value.load.side_effect = jobs
    builder = Mock(runner_cpu_request="75m", runner_memory_request="768Mi", node_selector={"role": "batch"}, tolerations=[])
    builder.dind_resource_strings.side_effect = [
        {"cpu_request": "125m", "memory_request": "64Mi"}, {"cpu_request": "2", "memory_request": "2Gi"},
    ]
    builders = Mock()
    builders.model_validate.return_value = builder
    task = Mock()
    task.return_value.config.environment = SimpleNamespace(cpus=2, memory_mb=2048, storage_mb=1024)
    monkeypatch.setitem(sys.modules, "fleet.harbor", SimpleNamespace(
        HarborJobConfigSource=source, HarborWorkflowBuilder=builders,
        PreparedHarborTask=SimpleNamespace(model_construct=SimpleNamespace),
    ))
    monkeypatch.setitem(sys.modules, "harbor.models.task.task", SimpleNamespace(Task=task))
    probe = runpy.run_path(str(Path(resources.__file__).with_name("resource_probe.py")))
    config = {"runner_cpu_request": "75m"}
    report = await probe["requests"]({"job_source": {}, "additional_jobs": [{"job_source": {}}]}, config)
    builders.model_validate.assert_called_once_with(config)
    dataset.get_task_configs.assert_awaited_once_with(disable_verification=True)
    assert task.call_count == 2
    assert report["runner"] == ["75m", "768Mi"] and report["selector"] == {"role": "batch"}
    assert report["tasks"] == {str(paths[0]): ["125m", "64Mi"], str(paths[1]): ["2", "2Gi"]}
    assert builder.dind_resource_strings.call_args.args[0].memory_mb == 2048
