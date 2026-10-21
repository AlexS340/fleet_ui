from datetime import datetime, timedelta, timezone

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


def test_argo_tasks_parse_and_urls() -> None:
    tasks = argo.parse_tasks(samples.TASKS_TEXT)
    assert [t.task for t in tasks] == ["t1-0", "t2-0", "t3-0"]
    ok, failed, running = tasks
    assert ok.phase == "Succeeded" and not ok.failed and not ok.active and ok.results_rel == "cowork/terminus-2/results/t1-0"
    assert failed.failed and failed.message == "pod failed" and running.active and running.finished is None
    now = datetime(2026, 10, 21, 5, 45, tzinfo=timezone.utc)
    assert ok.duration(now) == timedelta(minutes=20) and running.duration(now) == timedelta(minutes=15)
    assert argo.TaskWorkflow("wf", "Pending", "inst-0", "", None, None).task == "inst-0" and argo.TaskWorkflow("wf", "", "", "", None, None).duration(now) is None
    assert argo.workflow_url("https://argo/", NS, "h-x") == f"https://argo/workflows/{NS}/h-x"


async def test_argo_tasks_and_pod_logs_argv() -> None:
    fake = FakeRunner()
    samples.install(fake, NS)
    tasks = await argo.list_tasks(fake, NS, "run-a")
    assert len(tasks) == 3 and fake.calls[-1][0][:7] == ("kubectl", "get", "workflows", "-n", NS, "-l", "fleet/run-name=run-a")
    assert "fleet/results-instance" in fake.calls[-1][0][-1] and "fleet/results-rel" in fake.calls[-1][0][-1]
    log = await argo.pod_logs(fake, NS, "h-cowork-b02-z", "main", 50)
    assert "harbor run started" in log
    assert fake.calls[-1][0] == ("kubectl", "logs", "-n", NS, "-l", "workflows.argoproj.io/workflow=h-cowork-b02-z", "-c", "main", "--tail", "50", "--prefix")


async def test_mlsc_last_failed_log_goes_through_pod() -> None:
    fake = FakeRunner()
    fake.on("kubectl", "exec", "-i", "-n", "vllm-proxy", stdout='{"log": "CUDA out of memory"}')
    text = await mlsc.get_last_failed_log(fake, "vllm-proxy", "litellm-noauth-abc", "https://mlsc/mlc/", "tok", 41)
    assert text == "CUDA out of memory"
    argv, meta = fake.calls[-1]
    assert meta["stdin"] == "tok" and "tok" not in " ".join(argv)
    assert "get_last_failed_log?application_version_id=41" in argv[-1] and argv[-1].startswith("curl -sSf -k")
    fake.on("kubectl", "exec", "-i", "-n", "other", stdout="{}")
    assert await mlsc.get_last_failed_log(fake, "other", "p", "https://mlsc", "tok", 1) == ""


def test_s3_ls_parse_and_scan_results(tmp_path) -> None:
    from fleet_console.observe import s3

    objects = s3.parse_ls(samples.S3_LS, "swe-bench-v2-20260308/GLM/run-a/")
    assert len(objects) == 10 and objects[0].key == "cowork/terminus-2/results/t1-0/harbor_job.yaml" and objects[0].size == 120
    assert objects[1].is_text and not objects[6].is_text and objects[2].is_text  # .log, .tar.gz, COMPLETED
    results = s3.scan_results(objects)
    assert set(results) == {"cowork/terminus-2/results/t1-0", "cowork/terminus-2/results/t2-0", "cowork/terminus-2/results/t3-0"}
    t1, t2, t3 = (results[f"cowork/terminus-2/results/t{i}-0"] for i in (1, 2, 3))
    assert (t1.tries, t1.status, [f.key for f in t1.files]) == (1, "completed", ["COMPLETED", "runner.log"])
    assert (t2.tries, t2.status) == (2, "failed") and [f.key for f in t2.files] == ["FAILED", "polar_trajectories/polar_trajectories.tar.gz", "runner.log"]
    assert (t3.tries, t3.status, t3.files, t3.task) == (0, "no_attempt", [], "t3-0")
    assert s3.last_try(objects, "cowork/terminus-2/results/t2-0") == 2 and s3.last_try(objects, "cowork/terminus-2/results/t3-0") == 0
    # флаг без COMPLETED/FAILED → no_flag
    partial = s3.scan_results([s3.S3Object("b/a/results/t9-0/try1/runner.log", 1, "")])
    assert partial["b/a/results/t9-0"].status == "no_flag"

    cfg = tmp_path / "aws.cfg"
    env = s3.aws_env({"AWS_ACCESS_KEY_ID_gcvmfleet": "AK", "AWS_SECRET_ACCESS_KEY_gcvmfleet": "SK", "MLSC_TOKEN": "t"}, cfg, "https://obs.example", "ru-1")
    assert env == {"AWS_CONFIG_FILE": str(cfg), "AWS_PROFILE": "default", "AWS_ACCESS_KEY_ID": "AK", "AWS_SECRET_ACCESS_KEY": "SK"}
    assert cfg.read_text() == "[default]\nregion = ru-1\nendpoint_url = https://obs.example\ns3 =\n  addressing_style = virtual\n"
    assert "AWS_ACCESS_KEY_ID" not in s3.aws_env({}, cfg, "https://obs.example", "ru-1")  # без ключей aws сам скажет об ошибке


async def test_s3_ls_and_cat_commands() -> None:
    from fleet_console.observe import s3

    fake = FakeRunner()
    fake.on("aws", "s3", "ls", "--recursive", "s3://p-rndml-gcvmfleet/swe-bench-v2-20260308/GLM/run-a/", stdout=samples.S3_LS)
    fake.on("sh", "-c", stdout="line 1\nline 2\n")
    env = {"AWS_CONFIG_FILE": "/x/aws.cfg"}
    objects = await s3.ls(fake, "s3://p-rndml-gcvmfleet/swe-bench-v2-20260308/GLM/run-a", env)
    assert len(objects) == 10 and fake.calls[-1][1]["env"] == env
    text = await s3.cat(fake, "s3://p-rndml-gcvmfleet/x/runner.log", env, max_bytes=100)
    assert text == "line 1\nline 2\n" and fake.calls[-1][0] == ("sh", "-c", "aws s3 cp s3://p-rndml-gcvmfleet/x/runner.log - | head -c 100")
    long = FakeRunner()
    long.on("sh", "-c", stdout="x" * 100)
    assert (await s3.cat(long, "s3://b/k", env, max_bytes=100)).endswith("… (обрезано)")
