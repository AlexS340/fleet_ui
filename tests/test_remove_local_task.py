import asyncio
from pathlib import Path

import fakes
import pytest
import yaml
from nicegui.testing import User
from test_benches import Harness
from test_local_datasets import service, task

from fleet_console.compose import local_datasets
from fleet_console.compose.local_datasets import validate_folder
from fleet_console.runs import BenchError
from fleet_console.store import BenchRun


@pytest.fixture
def imported(tmp_path):
    svc = service(tmp_path)
    folder = tmp_path / "source"
    task(folder, "first")
    task(folder, "second")
    return svc, svc.import_folder(folder, "Sheet", owner_id="owner")


async def test_remove_one_task_from_uploaded_snapshot(imported, monkeypatch):
    svc, item = imported
    item.s3_uri, item.uploaded_at = f"s3://bucket/data/local/{item.id}/tasks/", "now"
    svc.save(item)
    svc.mapping(item)
    await svc.remove_task(item.id, "first", owner_id="owner")

    kept = svc.get(item.id)
    snapshot = Path(kept.path)
    report = validate_folder(snapshot)
    assert kept.tasks == ["second"] and kept.size == report.size < item.size
    assert kept.warnings and all("first" not in warning for warning in kept.warnings)
    assert kept.digest == report.digest != item.digest
    assert (Path(item.source) / "first/task.toml").is_file() and not (snapshot / "first").exists()
    assert kept.s3_uri != item.s3_uri and kept.s3_root == item.s3_root and not kept.uploaded_at
    mapping = yaml.safe_load((svc.overlay / "datasets" / f"{item.id}.yaml").read_text())
    assert mapping["runtime"]["task_s3_base"] == kept.s3_uri
    assert "отправьте" in svc.ready(item.id) and not svc.runner.calls
    monkeypatch.setattr(local_datasets, "secrets_env", lambda *args: {
        "AWS_ACCESS_KEY_ID_gcvmfleet": "key", "AWS_SECRET_ACCESS_KEY_gcvmfleet": "secret",
    })
    svc.runner.on("aws")
    await svc.upload(item.id, kept.s3_root)
    assert not svc.ready(item.id) and svc.runner.calls[-1][0][4] == kept.s3_uri
    with pytest.raises(ValueError, match="хотя бы одна"):
        await svc.remove_task(item.id, "second", owner_id="owner")


async def test_remove_task_checks_owner_name_and_busy_state(imported):
    svc, item = imported
    with pytest.raises(ValueError, match="владелец"):
        await svc.remove_task(item.id, "first", owner_id="other")
    with pytest.raises(ValueError, match="нет"):
        await svc.remove_task(item.id, "../first", owner_id="owner")
    svc.active_runs = lambda: [BenchRun("bench", "queued", "today", dataset=item.id)]
    with pytest.raises(ValueError, match="активным прогоном"):
        await svc.remove_task(item.id, "first", owner_id="owner")
    svc.active_runs = list
    svc.jobs[item.id] = asyncio.get_running_loop().create_future()
    with pytest.raises(ValueError, match="отправки в S3"):
        await svc.remove_task(item.id, "first", owner_id="owner")
    assert svc.get(item.id) == item and (Path(item.path) / "first/task.toml").is_file()


async def test_remove_task_rolls_back_if_record_write_fails(imported, monkeypatch):
    svc, item = imported
    item.s3_uri, item.uploaded_at = f"s3://bucket/data/local/{item.id}/tasks/", "now"
    svc.save(item)
    svc.mapping(item)

    def disk_full(_):
        raise OSError("disk full")

    monkeypatch.setattr(svc, "save", disk_full)
    with pytest.raises(OSError, match="disk full"):
        await svc.remove_task(item.id, "first", owner_id="owner")
    assert svc.get(item.id) == item and validate_folder(Path(item.path)).digest == item.digest
    assert item.s3_uri in (svc.overlay / "datasets" / f"{item.id}.yaml").read_text()


async def test_stale_preview_cannot_submit_after_task_removal(tmp_path):
    harness = Harness(tmp_path)
    svc = service(tmp_path)
    folder = tmp_path / "source"
    task(folder, "first")
    task(folder, "second")
    item = svc.import_folder(folder, "Sheet", owner_id="owner")
    origin = {"id": item.id, "s3_uri": item.s3_uri, "digest": item.digest}
    await svc.remove_task(item.id, "first", owner_id="owner")
    current = svc.get(item.id)
    current.uploaded_at = "republished"
    svc.save(current)
    with pytest.raises(BenchError, match="Состав локального набора изменился"):
        await harness.start(dataset_source=origin)
    assert not harness.store.benches()


@pytest.mark.nicegui_main_file("tests/ui_main.py")
async def test_remove_task_confirmation_updates_dialog_and_table(user: User, tmp_path):
    ctx = fakes.CTX
    ctx.local_datasets.overlay = tmp_path / "overlay"
    task(tmp_path, "first")
    task(tmp_path, "second")
    item = ctx.local_datasets.import_folder(tmp_path, "Две задачи")
    await user.open("/runs")
    user.find(marker="local-datasets").trigger("rowClick", [None, {"id": item.id}])
    user.find("Состав набора").click()
    select = user.find(marker="local-task").elements.pop()
    select.set_value("first")
    user.find("Удалить задачу").click()
    await user.should_see("Удалить задачу «first» из набора «Две задачи»?")
    user.find("Отмена").click()
    assert ctx.local_datasets.get(item.id) == item
    user.find("Удалить задачу").click()
    user.find("Да, удалить задачу").click()
    await user.should_see("Из локальной папки · задач: 1")
    assert select.options == ["second"]
    assert user.find(marker="local-datasets").elements.pop().rows[0]["tasks"] == 1
