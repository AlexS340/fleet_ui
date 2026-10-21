"""Происхождение значений resolved.yaml: порядок Hydra, @package, вложенные defaults, overrides, «вероятно»."""

from pathlib import Path

from fleet_console.compose.provenance import build_index, flatten, layers, lookup, package_of, parse_defaults, tree_nodes, value_at
from fleet_console.fleetref.profile import CONFIGS_REL

FILES = {
    "system/cluster.yaml": "# Стенд.\n\n# Namespace кластера; он же подкаталог в bucket.\nnamespace: swe-bench   # лейбл\nbucket: s3://b\n",
    "mlsc/common.yaml": "pod_name: litellm-abc\nstart_llm:\n  timeout: 10\n",
    "mlsc/job_base.yaml": "job:\n  image: base:1\n  queue_name: gigacode\n",
    "mlsc/model/qwen.yaml": "# @package mlsc\ndefaults:\n  - ../job_base@_here_\n  - _self_\n\n# Рецепт Qwen.\nserved_model_name: Qwen\ncreate_job:\n  job:\n    image: qwen:2\n",
    "harbor/agent/terminus2.yaml": "# @package _global_\n# Агент terminus-2.\nharbor_job:\n  agents:\n    - name: terminus-2\n      model_name: openai/${experiment.model_name}\n",
    "execution/production.yaml": "task_limit: null\nretry_limit: 2\n",
}
EXP = """# Прогон
defaults:
  - system: cluster
  - mlsc/common@mlsc
  - mlsc/model: qwen
  - harbor/agent: terminus2
  - execution: production
  - _self_

experiment:
  run_id: ???
  model_name: ${mlsc.served_model_name}
execution:
  task_limit: 3     # смоук
"""
RESOLVED = {
    "system": {"namespace": "swe-bench", "bucket": "s3://b"},
    "mlsc": {"pod_name": "litellm-abc", "start_llm": {"timeout": 10}, "job": {"image": "base:1", "queue_name": "gigacode"}, "served_model_name": "Qwen", "create_job": {"job": {"image": "qwen:2"}}},
    "harbor_job": {"agents": [{"name": "terminus-2", "model_name": "openai/Qwen"}]},
    "execution": {"task_limit": 3, "retry_limit": 2},
    "experiment": {"run_id": "run-1", "model_name": "Qwen"},
}


def make(tmp_path: Path) -> tuple[Path, Path, Path]:
    worktree, overlay, run_dir = tmp_path / "wt", tmp_path / "overlay", tmp_path / "run"
    for rel, text in FILES.items():
        path = worktree / CONFIGS_REL / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    overlay.mkdir()
    run_dir.mkdir()
    (run_dir / "exp.yaml").write_text(EXP, encoding="utf-8")
    return worktree, overlay, run_dir


def test_package_and_defaults_parsing() -> None:
    assert package_of("# @package _global_\na: 1", "harbor/agent") == ""
    assert package_of("# @package mlsc\na: 1", "mlsc/model") == "mlsc"
    assert package_of("a: 1", "harbor/agent") == "harbor.agent"
    assert package_of("# @package _group_\na: 1", "mlsc/model") == "mlsc.model"
    assert parse_defaults("a: 1") is None
    assert parse_defaults("defaults:\n  - system: cluster\n  - mlsc/common@mlsc\n  - ../job_base@_here_\n  - _self_\n  - harbor/dataset: ???\n") == [
        ("system/cluster", None), ("mlsc/common", "mlsc"), ("../job_base", "_here_"), ("_self_", None),
    ]


def test_layers_follow_hydra_order(tmp_path: Path) -> None:
    worktree, overlay, run_dir = make(tmp_path)
    got = [(layer.rel, layer.package, layer.origin) for layer in layers(EXP, run_dir, worktree, overlay)]
    assert got == [
        ("system/cluster.yaml", "system", "fleet"),
        ("mlsc/common.yaml", "mlsc", "fleet"),
        ("mlsc/job_base.yaml", "mlsc", "fleet"),  # ../job_base@_here_ → пакет родителя
        ("mlsc/model/qwen.yaml", "mlsc", "fleet"),
        ("harbor/agent/terminus2.yaml", "", "fleet"),
        ("execution/production.yaml", "execution", "fleet"),
        ("exp.yaml", "", "exp"),
    ]


def test_lookup_winner_comments_and_overrides(tmp_path: Path) -> None:
    worktree, overlay, run_dir = make(tmp_path)
    index = build_index(layers(EXP, run_dir, worktree, overlay), ["hydra.searchpath=[x]", "experiment.run_id=run-1"])

    ns = lookup("system.namespace", index, "swe-bench")
    assert ns.winner is not None and ns.winner.path.endswith("system/cluster.yaml") and ns.winner.line == 4 and ns.certain
    assert ns.winner.comments == "Namespace кластера; он же подкаталог в bucket." and ns.winner.inline == "лейбл" and ns.note == ""

    image = lookup("mlsc.create_job.job.image", index, "qwen:2")
    assert image.winner is not None and image.winner.path.endswith("mlsc/model/qwen.yaml") and image.winner.raw == "qwen:2"
    base = lookup("mlsc.job.image", index, "base:1")
    assert base.winner is not None and base.winner.path.endswith("mlsc/job_base.yaml")

    agent = lookup("harbor_job.agents[0].model_name", index, "openai/Qwen")
    assert agent.winner is not None and agent.winner.path.endswith("harbor/agent/terminus2.yaml") and agent.winner.interpolation and agent.certain
    assert agent.winner.raw == "openai/${experiment.model_name}"

    limit = lookup("execution.task_limit", index, 3)
    assert limit.winner is not None and limit.winner.origin == "exp" and limit.winner.inline == "смоук"
    assert [o.path for o in limit.others][-1].endswith("execution/production.yaml")

    run_id = lookup("experiment.run_id", index, "run-1")
    assert run_id.winner is not None and run_id.winner.origin == "override" and run_id.winner.raw == "run-1" and run_id.certain
    assert run_id.winner.where == "override командной строки · командная строка"

    section = lookup("mlsc", index, RESOLVED["mlsc"])
    assert section.winner is not None and section.winner.container and section.certain
    assert len(section.others) == 2 and section.winner.path.endswith("mlsc/model/qwen.yaml")  # common, job_base перекрыты

    assert lookup("nowhere.key", index, 1).winner is None and "не найдено" in lookup("nowhere.key", index, 1).note


def test_lookup_marks_probable_candidate(tmp_path: Path) -> None:
    worktree, overlay, run_dir = make(tmp_path)
    (run_dir / "execution" / "production.yaml").parent.mkdir(parents=True)
    (run_dir / "execution" / "production.yaml").write_text("task_limit: 9\nretry_limit: 2\n", encoding="utf-8")
    exp = EXP.replace("  task_limit: 3     # смоук\n", "  task_limit: ${oc.env:LIMIT}\n")
    index = build_index(layers(exp, run_dir, worktree, overlay), [])
    probable = lookup("execution.retry_limit", index, 5)  # ни один файл не даёт 5 → последний, но «вероятно»
    assert probable.winner is not None and probable.winner.origin == "run" and not probable.certain and "вероятно" in probable.note
    interp = lookup("execution.task_limit", index, 9)
    assert interp.winner is not None and interp.winner.origin == "exp" and interp.winner.interpolation and interp.certain


def test_flatten_tree_and_value_at() -> None:
    flat = flatten(RESOLVED)
    assert ("system.namespace", "swe-bench", False) in flat and ("harbor_job.agents[0]", {"name": "terminus-2", "model_name": "openai/Qwen"}, True) in flat
    assert value_at(RESOLVED, "harbor_job.agents[0].name") == "terminus-2" and value_at(RESOLVED, "harbor_job.agents[3].name") is None
    nodes = tree_nodes(RESOLVED)
    system = next(n for n in nodes if n["id"] == "system")
    assert system["children"][0] == {"id": "system.namespace", "label": "namespace: swe-bench"}
    agents = next(n for n in nodes if n["id"] == "harbor_job")["children"][0]
    assert agents["children"][0]["id"] == "harbor_job.agents[0]" and agents["children"][0]["label"] == "[0]"
