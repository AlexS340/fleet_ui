"""Сборка прогона без fleet и кластера: exp.yaml, overrides, реестр, превью из resolved.yaml, preflight, исходники."""

from datetime import UTC, datetime
from pathlib import Path

import yaml

from fleet_console.compose import (
    BenchSpec,
    SessionLink,
    build_registry,
    copy_for_edit,
    find_mapping,
    from_resolved,
    load_issues,
    make_bench_id,
    manual_edits,
    run_checks,
    sources,
    summary,
    unified_diff,
    validate_yaml,
)
from fleet_console.compose.preview import Preview
from fleet_console.connect.kube import KubeStatus
from fleet_console.fleetref.profile import CONFIGS_REL, Profile
from fleet_console.presets import DatasetRef
from fleet_console.settings import KNOWN_ISSUES

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
ALL_STEPS = [
    "system.materialize", "system.topology_saver", "system.manifest_runner", "mlsc.create_agent_limit", "system.runtime_upload",
    "system.task_artifacts_upload", "system.pod_upload", "system.submitter_launch", "system.measure_run", "system.requeue_failed",
    "mlsc.create_job", "mlsc.start_llm", "mlsc.check_llm_works", "mlsc.stop_llm",
]


def make_tree(tmp_path: Path) -> tuple[Path, Path]:
    wt, overlay = tmp_path / "wt", tmp_path / "overlay"
    cfg = wt / CONFIGS_REL
    files = {
        "harbor/agent/terminus2.yaml": "# @package _global_\n# Агент terminus-2.\nharbor_job:\n  agents:\n    - name: terminus-2\n",
        "harbor/dataset/birdsql_sqlite.yaml": "# @package _global_\nharbor_job:\n  datasets:\n    - path: ${system.datasets_root}/birdsql/sqlite/tasks\nharbor_dataset:\n  benchmark: birdsql-sqlite\n",
        "mlsc/model/glm.yaml": "# @package mlsc\nserved_model_name: GLM\n",
        "mlsc/common.yaml": "pod_name: p\n",
        "system/cluster.yaml": "namespace: ns\n",
        "execution/production.yaml": "task_limit: null\n",
        "harbor/job/default.yaml": "harbor_job: {}\n",
        "harbor/source/composed.yaml": "harbor_source: {}\n",
        "datasets/birdsql_sqlite.yaml": "priority: 12\nbenchmark: birdsql-sqlite\nsource:\n  tasks_root: birdsql/sqlite/tasks\nruntime:\n  task_s3_base: birdsql/sqlite/tasks\n",
        "datasets/swe.yaml": "benchmark: swe\nsource:\n  tasks_root: swe_bench_verified/tasks\nruntime:\n  task_s3_base: swe_bench_verified/tasks\n",
        "datasets/broken.yaml": "benchmark: x\n",
    }
    for rel, text in files.items():
        (cfg / rel).parent.mkdir(parents=True, exist_ok=True)
        (cfg / rel).write_text(text, encoding="utf-8")
    (overlay / "datasets").mkdir(parents=True)
    (overlay / "harbor" / "agent").mkdir(parents=True)
    (overlay / "datasets" / "cowork_r10.yaml").write_text(
        "benchmark: cowork\nsource:\n  tasks_root: cowork_bench_v2/tasks\nruntime:\n  task_s3_base: cowork/r10/tasks\n", encoding="utf-8"
    )
    (overlay / "harbor" / "agent" / "terminus2_cowork.yaml").write_text("# @package _global_\nharbor_job:\n  agents:\n    - name: terminus-2-cowork\n", encoding="utf-8")
    return wt, overlay


def spec_for(tmp_path: Path, **kw) -> BenchSpec:
    wt, overlay = make_tree(tmp_path)
    base = dict(
        run_id="smoke-20261021-120000", worktree=wt, run_dir=tmp_path / "runs" / "smoke", overlay=overlay, recipe="glm", agent="terminus2",
        dataset="birdsql_sqlite", jobs=2, agents_per_replica=5, task_limit=3, model_name="GLM-console", pod="litellm-abc", sha="378874b" * 5,
        harbor_job={"agent_timeout_multiplier": 2.0}, extra=("execution.retry_limit=0",), preset="smoke",
    )
    base.update(kw)
    return BenchSpec(**base)


def test_bench_id_fits_k8s_label() -> None:
    assert make_bench_id("qwen36-birdsql-smoke", T0) == "qwen36-birdsql-smoke-20261021-120000"
    long = make_bench_id("Очень длинное имя " + "x" * 80, T0)
    assert len(long) <= 63 and long.endswith("-20261021-120000") and long.startswith("x")


def test_exp_yaml_overrides_and_argv(tmp_path: Path) -> None:
    spec = spec_for(tmp_path)
    exp = spec.write()
    text = exp.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert data["defaults"][2] == {"mlsc/model": "glm"} and {"harbor/dataset": "birdsql_sqlite"} in data["defaults"]
    assert data["experiment"] == {"run_id": "???", "model_name": "${mlsc.served_model_name}", "jobs": 2, "agents_per_replica": 5, "workflow_prefix": "h-${harbor_dataset.benchmark}", "inference": "litellm"}
    assert data["execution"]["task_limit"] == 3 and data["mlsc"] == {"served_model_name": "GLM-console"}
    assert data["harbor_job"] == {"agent_timeout_multiplier": 2.0} and data["main"]["run"] == "???" and data["output_dir"] == "???"
    assert "# Прогон Fleet Console: smoke-20261021-120000 · пресет smoke" in text and "правка действует только в этом прогоне" in text
    assert (spec.run_dir / "fleet.sha").read_text().strip() == spec.sha and (spec.run_dir / "logs").is_dir()
    overrides = spec.overrides(["system.materialize", "system.topology_saver"])
    assert overrides[0] == f"hydra.searchpath=[file://{spec.worktree / CONFIGS_REL},file://{spec.overlay}]"
    assert f"experiment.run_id={spec.run_id}" in overrides and f"output_dir={spec.run_dir / 'out'}" in overrides
    assert f"system.materialize.dataset_registry.directory={spec.run_dir / 'registry'}" in overrides and "mlsc.pod_name=litellm-abc" in overrides
    assert "mlsc.create_job.job.application_name='fleet-console smoke-20261021-120000'" in overrides
    assert overrides[-2:] == ["execution.retry_limit=0", "main.run=[system.materialize,system.topology_saver]"]
    assert spec.overrides_file.read_text().count("\n") == len(spec.service_overrides()) + 1
    argv = spec.argv(["mlsc.create_job"], print_config=True)
    assert argv[:7] == ["uv", "run", "--project", str(spec.worktree), "--no-sync", "fleet", str(exp)] and argv[7] == "--print-config"
    # ручная правка exp.yaml не затирается повторной записью, force — затирает
    exp.write_text(text + "\n# моя правка\n", encoding="utf-8")
    spec.write()
    assert exp.read_text().endswith("# моя правка\n")
    spec.write(force=True)
    assert not exp.read_text().endswith("# моя правка\n")


def test_exp_yaml_for_session_and_inline_dataset(tmp_path: Path) -> None:
    session = SessionLink("console-glm-1", "GLM-s", "glm_session", 3, 1164)
    spec = spec_for(tmp_path, session=session, model_name="", dataset=DatasetRef(path="cowork_bench_v2/tasks", benchmark="cowork_bench", mapping="cowork_r10"))
    data = yaml.safe_load(spec.exp_text())
    assert {"mlsc/model": "glm_session"} in data["defaults"] and not any("harbor/dataset" in d for d in data["defaults"] if isinstance(d, dict))
    assert data["harbor_job"]["datasets"] == [{"path": "${system.datasets_root}/cowork_bench_v2/tasks", "n_tasks": "${execution.task_limit}"}]
    assert data["harbor_dataset"] == {"benchmark": "cowork_bench"} and "mlsc" not in data
    overrides = spec.service_overrides()
    assert "mlsc.served_model_name=GLM-s" in overrides and "experiment.jobs=3" in overrides
    assert spec.effective_jobs == 3 and spec.effective_model == "GLM-s" and spec.dataset_label == "cowork_bench_v2/tasks"


def test_registry_merge_and_mapping(tmp_path: Path) -> None:
    spec = spec_for(tmp_path)
    mappings, errors = build_registry(spec.worktree, spec.overlay, spec.run_dir)
    assert errors == [] and [m.name for m in mappings] == ["birdsql_sqlite", "swe", "cowork_r10"]  # broken.yaml без tasks_root пропущен, как в fleet
    assert sorted(p.name for p in spec.registry_dir.iterdir()) == ["birdsql_sqlite.yaml", "cowork_r10.yaml", "swe.yaml"]
    root = tmp_path / "datasets"
    (root / "birdsql" / "sqlite" / "tasks").mkdir(parents=True)
    found = find_mapping(mappings, root, root / "birdsql" / "sqlite" / "tasks")
    assert found and found.name == "birdsql_sqlite" and found.origin == "fleet"
    assert find_mapping(mappings, root, root / "other" / "tasks") is None
    # коллизии: то же имя в overlay и тот же tasks_root под другим именем
    (spec.overlay / "datasets" / "swe.yaml").write_text("benchmark: swe2\nsource:\n  tasks_root: elsewhere\nruntime:\n  task_s3_base: x\n")
    (spec.overlay / "datasets" / "dup.yaml").write_text("benchmark: dup\nsource:\n  tasks_root: birdsql/sqlite/tasks\nruntime:\n  task_s3_base: y\n")
    mappings, errors = build_registry(spec.worktree, spec.overlay, spec.run_dir)
    assert len(errors) == 2 and "имя swe" in errors[1] and "tasks_root birdsql/sqlite/tasks" in errors[0]
    assert [m.name for m in mappings] == ["birdsql_sqlite", "swe", "cowork_r10"]


RESOLVED = {
    "system": {
        "namespace": "swe-bench-v2-20260308", "agent_image": "gc-cli-harbor:v0.3.0", "s3_base_directory": "s3://b/ns/GLM-console/smoke",
        "pending_dir": "/manifests/harbor_run/smoke/pending",
        "polar": {"topology": {"gateway": {"nodes": [{"inference": {"base_url": "http://litellm.litellm-argo"}}]}}},
        "workflow_builder": {"node_selector": {"type": "bench"}},
    },
    "mlsc": {"served_model_name": "GLM-console", "create_job": {"job": {"image": "fleet-vllm:0.24.0", "instance_type_name": "a100plus.8gpu.80vG", "queue_name": "gigacode"}}},
    "experiment": {"run_id": "smoke", "model_name": "GLM-console", "jobs": 2, "agents_per_replica": 5, "inference": "litellm"},
    "harbor_job": {"agents": [{"name": "terminus-2"}], "datasets": [{"path": "/data/birdsql/sqlite/tasks", "n_tasks": 3, "task_names": ["bird_dev_0"]}], "agent_timeout_multiplier": 2.0, "verifier_timeout_multiplier": 2.0},
    "harbor_dataset": {"benchmark": "birdsql-sqlite"},
    "execution": {"active_deadline_seconds": 7200, "retry_limit": 1},
}


def test_preview_from_resolved(tmp_path: Path) -> None:
    (tmp_path / "out" / ".fleet").mkdir(parents=True)
    (tmp_path / "out" / ".fleet" / "tasks.json").write_text('[{"a": 1}, {"a": 2}, {"a": 3}]')
    p = from_resolved(RESOLVED, revision="master@378874b, влита", context="internal", run_dir=tmp_path)
    assert p.gpu_total == 16 and p.agent_limit == 10 and p.tasks_total == 3 and p.queue == "gigacode" and p.gateway.endswith("litellm-argo")
    rows = dict(p.rows())
    assert rows["GPU"] == "2 × a100plus.8gpu.80vG = 16 GPU" and rows["Параллелизм"] == "лимит 10 = 2 реплик × 5 агентов"
    assert rows["Задачи"].startswith("birdsql-sqlite: /data/birdsql/sqlite/tasks (первые 3) [1 по именам] · 3 задач · пул узлов type=bench")
    assert rows["Таймауты"] == "агент ×2.0 · верификатор ×2.0 · дедлайн пода 7200 с · retry Argo 1" and rows["Результаты"].startswith("s3://")
    again = Preview.from_json(p.to_json())
    assert again == p and Preview.from_json("") == Preview()
    empty = from_resolved({}, run_dir=tmp_path / "none")
    assert empty.gpu_total is None and "GPU на инстанс не распознан" in dict(empty.rows())["GPU"] and empty.tasks_total is None


def _profile(steps=ALL_STEPS, harbor="0.22.0") -> Profile:
    return Profile(sha="x", layout="harbor_new", print_config=True, steps=list(steps), harbor_version=harbor)


def test_preflight_checks(tmp_path: Path) -> None:
    root = tmp_path / "data"
    (root / "birdsql" / "sqlite" / "tasks").mkdir(parents=True)
    spec = spec_for(tmp_path)
    mappings, _ = build_registry(spec.worktree, spec.overlay, spec.run_dir)
    resolved = {**RESOLVED, "harbor_job": {**RESOLVED["harbor_job"], "datasets": [{"path": str(root / "birdsql" / "sqlite" / "tasks")}]}}
    kube = KubeStatus(context="internal", allowed=True, ready=True, permissions={"get nodes": True})
    issues = load_issues(KNOWN_ISSUES)
    assert issues and issues[0].id == "terminus2-mcp-harbor-0.22"
    common = dict(
        kube=kube, profile=_profile(), own_model=True, resolved=resolved, resolve_error="", datasets_root=root, mappings=mappings,
        registry_errors=[], foreign_models={"GLM"}, model_name="GLM-console", agent="terminus2", recipe="glm", dataset="birdsql_sqlite", issues=issues,
    )
    checks = run_checks(secrets={"MLSC_TOKEN": "t", "AWS_ACCESS_KEY_ID_gcvmfleet": "a", "AWS_SECRET_ACCESS_KEY_gcvmfleet": "s"}, **common)
    by = {c.key: c for c in checks}
    assert summary(checks) == (0, 2) and by["quota"].level == "warn" and by["issue:terminus2-mcp-harbor-0.22"].level == "warn"
    assert by["dataset"].level == "ok" and "tasks → birdsql_sqlite (fleet)" in by["dataset"].detail and by["model_name"].ok

    bad = run_checks(secrets={"MLSC_TOKEN": "t"}, **{**common, "kube": KubeStatus(context="other", allowed=False, error="context «other» не разрешён"),
                                                     "foreign_models": {"GLM-console"}, "profile": _profile(steps=ALL_STEPS[:-4]), "resolved": None, "resolve_error": "Hydra: missing"})
    by = {c.key: c for c in bad}
    assert by["secrets"].level == "fail" and "AWS_ACCESS_KEY_ID_gcvmfleet" in by["secrets"].detail
    assert by["kube"].level == "fail" and by["revision"].level == "fail" and "mlsc.create_job" in by["revision"].detail
    assert by["config"].level == "fail" and by["config"].detail == "Hydra: missing" and by["model_name"].level == "fail" and by["dataset"].level == "warn"
    assert summary(bad)[0] == 5

    on_session = run_checks(secrets={}, **{**common, "own_model": False, "profile": _profile(steps=ALL_STEPS[:9]), "session_status": "starting",
                                           "datasets_root": None, "resolved": {**resolved, "harbor_job": {"datasets": [{"path": "/nope"}]}}})
    by = {c.key: c for c in on_session}
    assert by["session"].level == "fail" and by["session"].detail == "starting" and "model_name" not in by
    assert by["revision"].level == "warn" and "requeue" in by["revision"].detail  # шаги модели сессии не нужны
    assert by["dataset"].level == "fail" and "FLEET_DATASETS_ROOT не задан" in by["dataset"].detail
    missing_dir = run_checks(secrets={}, **{**common, "datasets_root": tmp_path / "nope", "registry_errors": ["имя x дважды"]})
    by = {c.key: c for c in missing_dir}
    assert by["dataset"].level == "fail" and by["registry"].level == "fail"
    no_dir = run_checks(secrets={}, **{**common, "resolved": {**resolved, "harbor_job": {"datasets": [{"path": str(root / "missing")}]}}})
    assert "нет каталога" in {c.key: c for c in no_dir}["dataset"].detail


def test_sources_edit_and_diff(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, agent="terminus2_cowork")
    spec.write()
    text = spec.exp.read_text(encoding="utf-8")
    files = sources(text, spec.run_dir, spec.worktree, spec.overlay)
    assert [(f.rel, f.origin) for f in files][:3] == [("system/cluster.yaml", "fleet"), ("mlsc/common.yaml", "fleet"), ("mlsc/model/glm.yaml", "fleet")]
    agent = next(f for f in files if f.group == "harbor/agent")
    assert agent.origin == "overlay" and agent.title == "harbor/agent/terminus2_cowork.yaml · overlay сервиса"
    assert manual_edits(spec.run_dir, spec.worktree, spec.overlay, text) == []
    copy = copy_for_edit(agent, spec.run_dir)
    assert copy == spec.run_dir / "harbor" / "agent" / "terminus2_cowork.yaml" and copy.read_text() == agent.text
    copy.write_text(agent.text.replace("terminus-2-cowork", "terminus-2-mine"), encoding="utf-8")
    copy_for_edit(agent, spec.run_dir)  # повторная копия правку не затирает
    files = sources(text, spec.run_dir, spec.worktree, spec.overlay)
    assert next(f for f in files if f.group == "harbor/agent").origin == "run"
    edits = manual_edits(spec.run_dir, spec.worktree, spec.overlay, text)
    assert len(edits) == 1 and edits[0][0] == "harbor/agent/terminus2_cowork.yaml" and "-    - name: terminus-2-cowork" in edits[0][1] and "+    - name: terminus-2-mine" in edits[0][1]
    assert unified_diff("a\n", "a\n", "x") == "" and validate_yaml("a: [1") and validate_yaml("a: 1") is None
    assert "строка 1" in validate_yaml("a: b: c")
