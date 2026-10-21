from pathlib import Path

from fleet_console.catalog import CatalogEntry, group_order, scan
from fleet_console.catalog.scan import dataset_summary, header_comment
from fleet_console.fleetref.profile import CONFIGS_REL


def test_header_comment_strips_package_and_rulers() -> None:
    text = (
        "# @package _global_\n"
        "# =====================\n"
        "# Нативный Harbor AgentConfig.\n"
        "#   Второй абзац с отступом.\n"
        "# ---\n"
        "harbor_job:\n"
        "  agents: []\n"
        "# не шапка\n"
    )
    header, package = header_comment(text)
    assert package == "_global_"
    assert header == "Нативный Harbor AgentConfig.\n  Второй абзац с отступом."
    assert header_comment("key: 1\n") == ("", None)


def test_dataset_summary() -> None:
    assert dataset_summary("benchmark: cowork\nsource:\n  tasks_root: cowork/tasks\n") == "cowork · cowork/tasks"
    assert dataset_summary("priority: 20\n") == ""
    assert dataset_summary(": bad: [") == ""


def test_scan_groups_and_order(tmp_path: Path) -> None:
    cfg = tmp_path / CONFIGS_REL
    files = {
        "config.yaml": "# Точка входа smoke.\n# Вторая строка.\ndefaults: []\n",
        "harbor/agent/terminus2.yaml": "# @package _global_\n# Агент terminus-2 ходит в Polar gateway.\nharbor_job: {}\n",
        "mlsc/model/glm.yaml": "# @package mlsc\nserved_model_name: GLM\n",
        "datasets/cowork.yaml": "benchmark: cowork\nsource:\n  tasks_root: cowork/tasks\n",
        "zzz/custom.yaml": "# Неизвестная группа.\nx: 1\n",
        "mode/run.yaml": "# Один полный запуск.\nrun: []\n",
    }
    for rel, text in files.items():
        (cfg / rel).parent.mkdir(parents=True, exist_ok=True)
        (cfg / rel).write_text(text, encoding="utf-8")

    entries = scan(tmp_path)
    assert [e.group for e in entries] == ["mlsc/model", "harbor/agent", "datasets", "mode", "experiments", "zzz"]
    by_name = {e.name: e for e in entries}
    assert by_name["terminus2"].summary == "Агент terminus-2 ходит в Polar gateway."
    assert by_name["terminus2"].package == "_global_" and by_name["glm"].package == "mlsc"
    assert by_name["glm"].header == "" and by_name["glm"].summary == ""
    assert by_name["cowork"].summary == "cowork · cowork/tasks"
    assert by_name["config"].path == "examples/harbor_new/configs/config.yaml"
    assert by_name["config"].header == "Точка входа smoke.\nВторая строка."
    assert isinstance(entries[0], CatalogEntry)
    assert scan(tmp_path / "nope") == []


def test_group_order_unknown_groups_last_alphabetically() -> None:
    ranked = dict(group_order(["zzz", "mode", "aaa", "mlsc/model"]))
    assert ranked["mlsc/model"] < ranked["mode"] < ranked["aaa"] < ranked["zzz"]
