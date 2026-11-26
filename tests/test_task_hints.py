from pathlib import Path

import pytest
from nicegui.testing import User
from test_local_datasets import service, task

from fleet_console.compose import task_images
from fleet_console.compose.local_datasets import validate_folder


def author_sample(source: Path) -> None:
    for name in ["monthly", "weekly"]:
        path = task(source, name)
        for rel in ["task.toml", "tests/Dockerfile"]:
            image_file = path / rel
            image_file.write_text(image_file.read_text().replace("registry.example/", "registry.mlrnd.ru/"))
        with (path / "task.toml").open("a") as toml:
            toml.write('\n[task]\nname = "report"\n')
        (path / "tests/test.sh").write_bytes(b"echo TODO\r\n")
    env = source / "monthly/environment"
    (env / "Oracle").mkdir()
    for filename in ["golden.csv", "solution-data.json", "expected_answer.txt", "unanswered.md"]:
        (env / filename).touch()


def test_author_warnings_allow_import(tmp_path, monkeypatch):
    monkeypatch.setattr(task_images, "_probe", lambda *_: task_images.ImageIssue("Реестр недоступен"))
    source = tmp_path / "source"
    author_sample(source)
    svc = service(tmp_path)
    dataset = svc.import_folder(source, "Отчёты")
    warnings = svc.get(dataset.id).warnings
    for name in dataset.tasks:
        for hint in ["CRLF", "shebang", "проверьте verifier"]:
            assert any(f"{name}/tests/test.sh" in w and hint in w for w in warnings)
        assert any(f"{name}/solution/solve.sh" in w and "oracle-проверки" in w for w in warnings)
    assert any(all(s in w for s in ["monthly/task.toml", "weekly/task.toml", "[task].name", "report"]) for w in warnings)
    for filename in ["Oracle", "golden.csv", "solution-data.json", "expected_answer.txt"]:
        assert any(f"monthly/environment/{filename}" in w for w in warnings)
    assert not any("unanswered.md" in w for w in warnings)
    assert dataset.tasks == ["monthly", "weekly"]


@pytest.mark.parametrize("script,helper", [
    ("echo 1 > /logs/verifier/reward.txt", ""),
    ("echo '{}' | tee /logs/verifier/reward.json", ""),
    ('out=/logs/verifier/reward.txt\necho 1 > "$out"', ""),
    ("python /tests/verify.py", 'from pathlib import Path\nPath("/logs/verifier/reward.json").write_text("{}")'),
])
def test_visible_reward_and_oracle(tmp_path, script, helper):
    for name in ("one", "two"):
        path = task(tmp_path, name)
        (path / "tests/test.sh").write_text(f"#!/bin/sh\n{script}\n")
        (path / "tests/verify.py").write_text(helper)
        (path / "solution").mkdir()
        (path / "solution/solve.sh").write_text("#!/bin/sh")
    report = validate_folder(tmp_path)
    assert report.ok and not report.warnings


@pytest.mark.parametrize("body", [
    "# echo 1 > /logs/verifier/reward.txt\necho done",
    "cat /logs/verifier/reward.txt",
    "echo 1 > /tmp/reward.json",
])
def test_unused_helper_and_comments_do_not_hide_missing_reward(tmp_path, body):
    path = task(tmp_path)
    (path / "tests/test.sh").write_text(f"#!/bin/sh\n{body}\n")
    (path / "tests/unused.sh").write_text("echo 1 > /logs/verifier/reward.txt")
    assert any("проверьте verifier" in w for w in validate_folder(path).warnings)


@pytest.mark.nicegui_main_file("tests/ui_main.py")
async def test_import_dialog_shows_all_author_hints(user: User, tmp_path, monkeypatch):
    monkeypatch.setattr(task_images, "_probe", lambda *_: task_images.ImageIssue("Реестр недоступен"))
    author_sample(tmp_path)
    await user.open("/runs")
    user.find(marker="local-add").click()
    await user.should_see("Добавить локальные задачи")
    user.find(marker="local-folder").elements.pop().set_value(str(tmp_path))
    user.find(marker="local-check").click()
    await user.should_see("Минимальная проверка пройдена")
    user.find("Подсказки авторам").click()
    await user.should_see("weekly/task.toml")
    assert user.find(marker="local-import").elements.pop().enabled
