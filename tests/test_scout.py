from pathlib import Path

from fleet_console.presets import DatasetRef, load_presets
from fleet_console.presets.scout import CONFIGS, UNFILTER, parse_cat_batch, render_preset, scout, suggest, tasks_word

BASE = {
    "mlsc/model/qwen36_35b_a3b.yaml": "served_model_name: Qwen3.6-35B-A3B\n",
    "mlsc/model/checkpoints_qwen.yaml": "served_model_name: Qwen3-Coder-Next\n",
    "harbor/agent/terminus2.yaml": "harbor_job: {}\n",
    "harbor/agent/uag_minimal_20260912.yaml": "harbor_job: {}\n",
    "harbor/agent/qwen_coder.yaml": "harbor_job: {}\n",
    "execution/production.yaml": "task_limit: null\n",
    "config.yaml": "defaults:\n  - harbor/agent: terminus2\n  - harbor/dataset: birdsql_sqlite\n",
}
COWORK = {
    "datasets/cowork_bench_ua_external_260902.yaml": (
        "benchmark: cowork_bench_ua_external_260902\nsource:\n  tasks_root: cowork_bench_ua_external_260902/tasks\n"
        "runtime:\n  task_s3_base: cowork_bench_ua_external_260902/tasks\n"
    ),
    "exp_qwen36_cowork_1c.yaml": (
        "defaults:\n  - mlsc/common@mlsc\n  - mlsc/model: qwen36_35b_a3b\n  - harbor/agent: uag_minimal_20260912\n"
        "harbor_job:\n  datasets:\n    - path: ${system.datasets_root}/cowork_bench_ua_external_260902/tasks\n"
        "    - path: ${system.datasets_root}/1cbench_agentic/tasks\n"
    ),
    "harbor/suite/many.yaml": (
        "harbor_job:\n  datasets:\n    - path: ${system.datasets_root}/swe_bench_verified/tasks\n"
        "    - path: ${system.datasets_root}/cowork_bench_ua_external_260902/tasks\n"
        "    - path: ${system.datasets_root}/terminal-bench-2-1/tasks\n"
    ),
    "first_priority.yaml": "defaults:\n  - mlsc/model: checkpoints_qwen\n  - harbor/agent: terminus2\n  - harbor/suite: many\n",
}
BIRDSQL = {
    "harbor/dataset/birdsql_sqlite.yaml": (
        "harbor_job:\n  datasets:\n    - path: ${system.datasets_root}/birdsql/sqlite/tasks\n      task_names:\n        - bird_dev_0\n"
        "harbor_dataset:\n  benchmark: birdsql-sqlite\n"
    ),
    "harbor/dataset/unique_images.yaml": (
        "harbor_job:\n  datasets:\n    - path: /data/someone/birdsql/sqlite/tasks\n    - path: /data/someone/swe/tasks\n"
        "harbor_dataset:\n  benchmark: unique-images\n"
    ),
    "datasets/birdsql_sqlite.yaml": "benchmark: birdsql-sqlite\nsource:\n  tasks_root: birdsql/sqlite/tasks\n",
    "exp_qwen36_birdsql.yaml": "defaults:\n  - mlsc/model: qwen36_35b_a3b\n  - harbor/agent: qwen_coder\n  - harbor/dataset: birdsql_sqlite\n",
}


def files(*parts: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (BASE, *parts):
        out |= {CONFIGS + rel: text for rel, text in part.items()}
    return out


def test_registry_only_bench_becomes_path_preset_with_agent_from_its_experiment() -> None:
    s = scout("cowork", files(COWORK), ["main", "mashkurat/cowork-strands", "feat/x"], overlay=None)
    assert s.dataset_groups == [] and [e.name for e in s.entries] == ["cowork_bench_ua_external_260902"]
    assert [(e.file, e.agent, e.datasets) for e in s.experiments] == [
        ("exp_qwen36_cowork_1c.yaml", "uag_minimal_20260912", 2),
        ("first_priority.yaml", "terminus2", 3),
    ]
    assert s.branches == ["mashkurat/cowork-strands"]
    data, warnings = suggest(s)
    assert warnings == []
    assert data is not None and data["name"] == "qwen36-cowork-smoke" and data["model_name"] == "Qwen3.6-35B-A3B-console"
    assert data["agent"] == "uag_minimal_20260912" and data["execution"]["task_limit"] == 3 and "overrides" not in data
    # benchmark короткий и без подчёркиваний: идёт в generate_name workflow; метку результатов fleet берёт из реестра
    assert DatasetRef.model_validate(data["dataset"]) == DatasetRef(
        path="cowork_bench_ua_external_260902/tasks", benchmark="cowork", mapping="cowork_bench_ua_external_260902"
    )


def test_group_bench_uses_group_unfilters_task_names_and_skips_foreign_group() -> None:
    s = scout("birdsql", files(BIRDSQL), [], overlay=None)
    data, warnings = suggest(s, tasks=5)
    assert data is not None and data["dataset"] == "birdsql_sqlite" and data["overrides"] == [UNFILTER]
    assert data["agent"] == "qwen_coder"  # эксперимент с той же моделью важнее базового config.yaml
    assert data["description"].endswith("1 реплика, 5 задач")
    assert [g.name for g in s.dataset_groups] == ["birdsql_sqlite"]  # unique_images на два бенча — не по путям внутри
    assert not any("не годится" in w for w in warnings)
    assert any("не годится" in w and "и ещё 1" in w for w in suggest(scout("unique_images", files(BIRDSQL), [], overlay=None))[1])
    assert not any("кандидатов несколько" in w for w in warnings)  # запись реестра того же каталога — не второй кандидат


def test_unusable_group_falls_back_to_registry_and_explicit_agent_is_flagged() -> None:
    bad = {"harbor/dataset/cowork_ab.yaml": "harbor_job:\n  datasets:\n    - path: /data/m/cowork/dataset\nharbor_dataset:\n  benchmark: cowork_bench\n"}
    s = scout("cowork", files(COWORK, bad), [], overlay=None)
    data, warnings = suggest(s, agent="terminus2", model="checkpoints_qwen", tasks=1)
    assert data is not None and isinstance(data["dataset"], dict) and data["dataset"]["mapping"] == "cowork_bench_ua_external_260902"
    assert data["name"] == "checkpoints-cowork-smoke" and data["model_name"] == "Qwen3-Coder-Next-console"
    problems = next(w for w in warnings if "cowork_ab" in w)
    assert "путь не под FLEET_DATASETS_ROOT" in problems and "benchmark «cowork_bench»" in problems
    assert not any("terminus2 не из эксперимента" in w for w in warnings)  # first_priority с этим бенчем на terminus2
    data, warnings = suggest(s, agent="qwen_coder")
    assert any("qwen_coder не из эксперимента" in w for w in warnings)


def test_unknown_bench_gives_nothing_to_run() -> None:
    s = scout("nosuch", files(COWORK, BIRDSQL), ["nosuch-wip"], overlay=None)
    data, warnings = suggest(s)
    assert data is None and s.branches == ["nosuch-wip"] and "нет ни группы" in warnings[-1]


def test_unknown_model_or_agent_is_reported_by_compatibility() -> None:
    s = scout("cowork", files(COWORK), [], overlay=None)
    _, warnings = suggest(s, model="glm", agent="nope")
    assert "в ревизии нет mlsc/model=glm" in warnings and "в ревизии нет harbor/agent=nope" in warnings


def test_overlay_members_count_as_groups(tmp_path: Path) -> None:
    (tmp_path / "harbor" / "agent").mkdir(parents=True)
    (tmp_path / "harbor" / "agent" / "my_agent.yaml").write_text("harbor_job: {}\n")
    s = scout("cowork", files(COWORK), [], overlay=tmp_path)
    _, warnings = suggest(s, agent="my_agent")
    assert not any("в ревизии нет" in w for w in warnings)


def test_rendered_preset_loads_as_preset(tmp_path: Path) -> None:
    s = scout("cowork", files(COWORK), [], overlay=None)
    data, _ = suggest(s)
    assert data is not None
    text = render_preset(data, s, "master", "18726c6fe22b")
    assert text.startswith("# Собран fleet_console.presets.scout из master 18726c6f: реестр datasets/cowork_bench_ua_external_260902.yaml, агент из exp_qwen36_cowork_1c.yaml.")
    (tmp_path / "p.yaml").write_text(text, encoding="utf-8")
    presets, errors = load_presets(tmp_path)
    assert errors == [] and presets[0].name == "qwen36-cowork-smoke" and isinstance(presets[0].dataset, DatasetRef)


def test_parse_cat_batch_and_tasks_word() -> None:
    out = b"aaa blob 4\nx: 1\nbbb blob 0\n\nccc blob 8\ny: \xd0\xb4\xd0\xb0\n\n"
    assert parse_cat_batch(out) == ["x: 1", "", "y: да\n"]
    assert [tasks_word(n) for n in (1, 3, 5, 11, 21, 22, 112)] == ["1 задача", "3 задачи", "5 задач", "11 задач", "21 задача", "22 задачи", "112 задач"]
