from datetime import datetime, timezone

import cluster_samples as samples
from fleet_console.observe import ClusterObserver
from fleet_console.observe import argo, mlsc, nginx, nodes, submitter
from fleet_console.runner import FakeRunner, Result
from fleet_console.settings import Settings

NS = "swe-bench-v2-20260308"


def test_argo_groups_runs_newest_first() -> None:
    runs = argo.group(argo.parse(samples.ARGO_TEXT))
    assert [r.run_name for r in runs] == ["run-a", "run-b", argo.NO_RUN]
    a, b, _ = runs
    assert (a.running, a.pending, a.succeeded, a.failed, a.active, a.total) == (1, 1, 1, 0, 2, 3)
    assert a.model == "GLM" and a.started == datetime(2026, 10, 21, 4, tzinfo=timezone.utc)
    assert a.last_finished == datetime(2026, 10, 21, 4, 30, tzinfo=timezone.utc)
    assert (b.failed, b.active) == (2, 0)
    assert argo.argo_ui_url("http://127.0.0.1:2746/", NS, "run-a") == f"http://127.0.0.1:2746/workflows/{NS}?label=fleet%2Frun-name%3Drun-a"


def test_mlsc_active_versions_and_gpus() -> None:
    import json

    versions = mlsc.active_versions(json.loads(samples.CONFIGURATION))
    assert [v.version_id for v in versions] == [41, 42]  # версия 40 с 0/0 не живая
    glm, qwen = versions
    assert glm.model == "GLM-5.3-Flash" and glm.gpu_per_instance == 8 and glm.gpus == 24  # current=3
    assert qwen.model == "Qwen3" and qwen.gpus == 4
    assert mlsc.model_name({"job_command_line": "MODEL_PATH=/models/deepseek-v4/ serve"}) == "deepseek-v4"


async def test_mlsc_token_goes_through_stdin_and_pod_is_found_by_deployment() -> None:
    fake = FakeRunner()
    samples.install(fake)
    pod = await mlsc.find_pod(fake, "vllm-proxy", "litellm-noauth")
    assert pod == "litellm-noauth-abc"
    assert ("-l", "app=litellm-noauth") in zip(fake.calls[-1][0], fake.calls[-1][0][1:])
    conf = await mlsc.get_configuration(fake, "vllm-proxy", pod, "https://mlsc/mlc/", "SECRET\n")
    argv, meta = fake.calls[-1]
    assert "application_versions" in conf
    assert meta["stdin"] == "SECRET" and not any("SECRET" in a for a in argv)
    assert "$(cat)" in argv[-1] and "https://mlsc/mlc/api/v1/service/get_configuration" in argv[-1]


def test_nginx_upstreams() -> None:
    assert nginx.parse_upstreams(samples.NGINX_CONF) == {"GLM-5.3-Flash": 2, "Qwen3": 1}


def test_submitter_parse() -> None:
    a, b = submitter.parse(samples.SUBMITTER_TEXT)
    assert (a.run_id, a.limit, a.pending, a.submitted, a.completed, a.alive) == ("run-a", 40, 12, 30, 5, True)
    assert a.log_at == datetime.fromtimestamp(1761022800, tz=timezone.utc)
    assert b.limit is None and not b.alive
    assert "submit_workflows[.]py" in submitter.SCRIPT  # grep не должен находить собственную команду


def test_nodes_parse_and_quantities() -> None:
    assert nodes.parse_quantity("95500m") == 95.5 and nodes.parse_quantity("1456Gi") == 1456 * 2**30
    pool = nodes.parse_nodes(samples.NODES_TEXT, pending_pods=2)
    assert (pool.total, pool.ready, pool.unschedulable, pool.pending_pods) == (3, 2, 1, 2)
    assert round(pool.cpu_cores, 1) == 191.5 and 2800 < pool.memory_gib < 2900  # только готовые узлы


async def test_observer_isolates_source_errors() -> None:
    fake = FakeRunner()
    samples.install(fake)
    fake.handlers.insert(0, (("kubectl", "-n", "vllm-proxy", "get", "configmap"), Result(("x",), 1, "", "forbidden")))
    obs = ClusterObserver(fake, Settings(), mlsc_token=lambda: "t")
    await obs.refresh()
    assert obs.version == 5 and not obs.busy  # по единице на источник
    assert obs.nginx.error and "forbidden" in obs.nginx.error and obs.nginx.data is None
    assert [r.run_name for r in obs.runs.data][:2] == ["run-a", "run-b"]
    assert [v.version_id for v in obs.mlsc.data] == [41, 42] and obs.mlsc.error == ""
    assert obs.submitter.data[0].run_id == "run-a" and obs.nodes.data.total == 3
    assert all(s.at is not None for s in obs.sources)
    assert await obs.submitter_log("run-a") == "[1/3] submitted wf-a-1\n"


async def test_observer_without_token_and_pod_rediscovery() -> None:
    fake = FakeRunner()
    samples.install(fake)
    obs = ClusterObserver(fake, Settings(), mlsc_token=lambda: None)
    await obs.refresh()
    assert "MLSC_TOKEN" in obs.mlsc.error
    obs.mlsc_token = lambda: "t"
    await obs.refresh()
    assert obs.mlsc.error == "" and obs._mlsc_pod == "litellm-noauth-abc"
    fake.handlers.insert(0, (("kubectl", "exec", "-i"), Result(("x",), 1, "", "pod not found")))
    await obs.refresh()
    assert "pod not found" in obs.mlsc.error and obs._mlsc_pod is None  # под ищется заново
