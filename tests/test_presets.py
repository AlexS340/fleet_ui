from pathlib import Path

from fleet_console.presets import DatasetRef, Preset, compatibility, load_presets, matches_ref
from fleet_console.settings import PRESETS_DIR

GROUPS = {
    "mlsc/model": ["qwen36_35b_a3b", "glm"],
    "harbor/agent": ["terminus2", "uag_minimal_no_skills_0.3.0", "uag_minimal_20260912"],
    "harbor/dataset": ["birdsql_sqlite", "swe_bench_verified"],
    "execution": ["production", "debug"],
    "datasets": ["cowork_bench_r10", "cowork_bench_ua_external_260902"],
}
STEPS = ["system.materialize", "mlsc.create_job"]


def test_repo_presets_load_and_fit_master_groups() -> None:
    presets, errors = load_presets(PRESETS_DIR)
    assert errors == [] and {p.name for p in presets} >= {"qwen36-birdsql-smoke", "qwen36-swe-verified"}
    smoke = next(p for p in presets if p.name == "qwen36-birdsql-smoke")
    assert smoke.execution.task_limit == 3 and smoke.resources.agents_per_replica == 3 and smoke.dataset_label == "birdsql_sqlite"
    assert smoke.harbor_job["agent_timeout_multiplier"] == 2.0 and smoke.path.endswith("qwen36-birdsql-smoke.yaml")
    assert compatibility(smoke, GROUPS, STEPS, STEPS) == []
    assert matches_ref(smoke, "master", "378874b") and not matches_ref(smoke, "feat/x", "de460aa")
    cowork = next(p for p in presets if p.name == "qwen36-cowork-smoke")
    assert cowork.dataset == DatasetRef(path="cowork_bench_ua_external_260902/tasks", benchmark="cowork", mapping="cowork_bench_ua_external_260902")
    assert cowork.agent == "uag_minimal_20260912" and cowork.execution.task_limit == 3
    assert compatibility(cowork, GROUPS, STEPS, STEPS) == []


def test_broken_preset_is_reported_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "good.yaml").write_text("model: glm\nagent: terminus2\ndataset:\n  path: cowork_bench_v2\n  benchmark: cowork_bench\n  mapping: cowork_bench_r10\n")
    (tmp_path / "bad.yaml").write_text("model: glm\n")  # нет agent и dataset
    (tmp_path / "list.yaml").write_text("- a\n")
    presets, errors = load_presets(tmp_path)
    assert [p.name for p in presets] == ["good"] and len(errors) == 2 and errors[0].startswith("bad.yaml")
    good = presets[0]
    assert isinstance(good.dataset, DatasetRef) and good.dataset_label == "cowork_bench_v2" and good.fleet.ref == "master"
    assert compatibility(good, GROUPS, STEPS, STEPS) == []
    good.dataset.mapping = "missing"
    problems = compatibility(good, GROUPS, STEPS, ["system.measure_run"])
    assert problems == ["в реестре datasets нет missing", "в ревизии нет шагов: system.measure_run"]
    other = Preset(name="x", model="nope", agent="terminus2", dataset="nope", fleet={"ref": "release/*"})
    assert compatibility(other, GROUPS, STEPS, STEPS) == ["в ревизии нет mlsc/model=nope", "в ревизии нет harbor/dataset=nope"]
    assert matches_ref(other, "release/1", "abc") and not matches_ref(other, "master", "abc")
