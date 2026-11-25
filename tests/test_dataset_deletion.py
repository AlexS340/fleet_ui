import json
from pathlib import Path

import fakes
import pytest
from nicegui.testing import User
from test_benches import Harness
from test_local_datasets import service, task

from fleet_console.presets import DatasetRef
from fleet_console.runs import BenchError
from fleet_console.store import BenchRun


async def test_delete_only_snapshot_and_only_after_upload(tmp_path: Path) -> None:
    svc = service(tmp_path)
    source = task(tmp_path / "source")
    item = svc.import_folder(source, "first", owner_id="alice")
    other = svc.import_folder(source, "second", owner_id="alice")
    mapping = svc.overlay / "datasets" / f"{item.id}.yaml"
    with pytest.raises(ValueError, match="владелец"):
        svc.delete(item.id, owner_id="bob")
    upload = svc.upload(item.id, "s3://test/datasets")
    with pytest.raises(ValueError, match="отправки"):
        svc.delete(item.id, owner_id="alice")
    await upload
    assert svc.get(item.id).s3_uri and mapping.exists() and Path(item.path).is_dir()
    calls = list(svc.runner.calls)
    svc.delete(item.id, owner_id="alice")
    assert not mapping.exists() and not Path(item.path).parent.exists()
    assert svc.all() == [other] and Path(other.path).is_dir()
    assert (source / "environment/data.xlsx").read_text() == "example"
    assert svc.runner.calls == calls


def test_delete_can_retry_after_mapping_cleanup_fails(tmp_path: Path) -> None:
    svc = service(tmp_path)
    item = svc.import_folder(task(tmp_path / "source"), "sheet", owner_id="alice")
    mapping = svc.overlay / "datasets" / f"{item.id}.yaml"
    mapping.unlink()
    mapping.mkdir()
    with pytest.raises(OSError):
        svc.delete(item.id, owner_id="alice")
    assert svc.get(item.id) == item
    mapping.rmdir()
    svc.delete(item.id, owner_id="alice")
    assert not svc.all() and Path(item.source).is_dir()


def test_replaced_snapshot_cannot_delete_original(tmp_path: Path) -> None:
    svc = service(tmp_path)
    source = task(tmp_path / "source")
    item = svc.import_folder(source, "sheet", owner_id="alice")
    snapshot = Path(item.path)
    snapshot.rename(snapshot.with_name("old"))
    snapshot.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="путь"):
        svc.delete(item.id, owner_id="alice")
    assert svc.get(item.id) == item and (source / "task.toml").exists()


@pytest.mark.parametrize("status", ["queued", "running", "attention", "stopping"])
def test_active_run_protects_dataset_of_another_owner(tmp_path: Path, status: str) -> None:
    ctx = fakes.make_context()
    item = ctx.local_datasets.import_folder(task(tmp_path), "sheet", owner_id="alice")
    run = BenchRun("job", status, "now", dataset="group", owner_id="bob",
                   preview_json=json.dumps({"datasets": [{"path": item.path}]}))
    ctx.store.create_bench(run)
    with pytest.raises(ValueError, match="активным прогоном"):
        ctx.local_datasets.delete(item.id, owner_id="alice")
    # В старых записях путь может быть только в ссылке на датасет.
    ref = DatasetRef(path=f"local/{item.id}/tasks", benchmark="custom")
    ctx.store.update_bench(run.id, dataset=ref.model_dump_json(), preview_json="{}")
    with pytest.raises(ValueError, match="активным прогоном"):
        ctx.local_datasets.delete(item.id, owner_id="alice")
    ctx.store.update_bench(run.id, status="done")
    ctx.local_datasets.delete(item.id, owner_id="alice")
    assert ctx.store.get_bench(run.id).status == "done"


async def test_submission_rechecks_deleted_snapshot_before_enqueue(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    dataset = DatasetRef(path="local/local-sheet/tasks", benchmark="sheet")
    snapshot = Path(harness.settings.datasets_root) / dataset.path
    snapshot.mkdir(parents=True)
    print_config = harness.mgr.print_config

    async def deleting_print_config(spec):
        result = await print_config(spec)
        snapshot.rmdir()
        return result

    harness.mgr.print_config = deleting_print_config
    with pytest.raises(BenchError, match="копия набора недоступна"):
        await harness.start(dataset=dataset)
    assert not harness.store.benches()


@pytest.mark.nicegui_main_file("tests/ui_main.py")
async def test_confirmation_cancel_and_recheck(user: User, tmp_path: Path) -> None:
    ctx = fakes.CTX
    item = ctx.local_datasets.import_folder(task(tmp_path), "Образец")
    await user.open("/runs")
    user.find(marker="local-datasets").trigger("rowClick", [None, {"id": item.id}])
    user.find("Удалить набор").click()
    await user.should_see("Исходная папка и файлы в S3 сохранятся")
    user.find("Отмена").click()
    assert ctx.local_datasets.get(item.id) == item
    user.find("Удалить набор").click()
    run = BenchRun("late-job", "queued", "now", dataset=item.id)
    ctx.store.create_bench(run)
    user.find("Да, удалить набор").click()
    await user.should_see("Набор используется активным прогоном")
    assert Path(item.path).exists()
    ctx.store.update_bench(run.id, status="stopped")
    user.find("Да, удалить набор").click()
    await user.should_see("Готово")
    assert not ctx.local_datasets.all()
