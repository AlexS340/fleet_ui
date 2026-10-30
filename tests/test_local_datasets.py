"""Свои задачи: только tmp_path и FakeRunner, без Harbor, S3 и кластера."""

from pathlib import Path

import pytest
import yaml

from fleet_console.compose import local_datasets as local_module
from fleet_console.compose.local_datasets import LocalDatasets, validate_folder
from fleet_console.compose.registry import build_registry, find_mapping
from fleet_console.runner import FakeRunner, Result
from fleet_console.settings import Paths, Settings


def task(root: Path, name: str = "one") -> Path:
    path = root / name
    for rel, text in {
        "task.toml": 'schema_version = "1.1"\nartifacts = ["/app/data.xlsx"]\n[environment]\ndocker_image = "registry.example/sheet:v2"\n[verifier]\nenvironment_mode = "separate"\n[verifier.environment]\ncpus = 2\n',
        "instruction.md": "Edit /app/data.xlsx",
        "tests/test.sh": "#!/bin/sh\necho 1 > /logs/verifier/reward.txt",
        "tests/Dockerfile": "FROM registry.example/sheet:v2\nCOPY . /tests",
        "environment/data.xlsx": "example",
    }.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return path


def service(tmp_path: Path) -> LocalDatasets:
    settings = Settings(datasets_root=str(tmp_path / "data"))
    return LocalDatasets(FakeRunner(), Paths(tmp_path / "home").ensure(), settings, tmp_path / "overlay", lambda *args: None)


def test_validation_and_language_container(tmp_path: Path) -> None:
    a = task(tmp_path / "en")
    task(tmp_path / "en", "two")
    report = validate_folder(tmp_path / "en")
    assert report.ok and len(report.tasks) == 2 and report.size and report.digest
    assert validate_folder(a).ok
    report = validate_folder(tmp_path)
    assert not report.ok and "непосредственно" in report.errors[0]
    assert not validate_folder(tmp_path / "missing").ok


def test_rejects_bad_toml_images_and_verifier(tmp_path: Path) -> None:
    a = task(tmp_path)
    (a / "task.toml").write_text("[broken")
    assert not validate_folder(tmp_path).ok
    (a / "task.toml").write_text('[environment]\ncpus = 1\n[verifier]\nenvironment_mode = "separate"\n[verifier.environment]\ncpus = 1\n')
    (a / "tests/Dockerfile").unlink()
    (a / "instruction.md").write_text("")
    errors = "\n".join(validate_folder(tmp_path).errors)
    assert "docker_image" in errors and "tests/Dockerfile" in errors and "instruction.md" in errors
    b = task(tmp_path, "valid")
    assert validate_folder(b).ok and not validate_folder(tmp_path).ok  # не импортируем частично


def test_top_level_links_supported_but_internal_links_rejected(tmp_path: Path) -> None:
    a = task(tmp_path / "original")
    view = tmp_path / "view"
    view.mkdir()
    (view / "one").symlink_to(a, target_is_directory=True)
    assert validate_folder(view).ok
    (a / "environment/link").symlink_to(tmp_path / "original", target_is_directory=True)
    assert "ссылка внутри" in "\n".join(validate_folder(view).errors)


def test_import_is_snapshot_and_registry_survives_restart(tmp_path: Path) -> None:
    a = task(tmp_path / "source")
    svc = service(tmp_path)
    item = svc.import_folder(a.parent, "Мои задачи")
    assert len(svc.all()) == 1 and item.source == str(a.parent)
    assert "отправьте" in svc.ready(item.id)
    (a / "environment/data.xlsx").write_text("changed original")
    assert (Path(item.path) / "one/environment/data.xlsx").read_text() == "example"
    assert service(tmp_path).get(item.id) == item
    mappings, errors = build_registry(tmp_path / "wt", svc.overlay, tmp_path / "run")
    assert not errors and find_mapping(mappings, tmp_path / "data", Path(item.path)).name == item.id


async def test_upload_failure_retry_and_ready(tmp_path: Path, monkeypatch) -> None:
    svc = service(tmp_path)
    task(tmp_path / "source")
    item = svc.import_folder(tmp_path / "source", "sheet")
    monkeypatch.setattr(local_module, "secrets_env", lambda *args: {
        "AWS_ACCESS_KEY_ID_gcvmfleet": "secret-key", "AWS_SECRET_ACCESS_KEY_gcvmfleet": "secret-token",
    })
    svc.runner.on("aws", result=Result((), 1, "", "AccessDenied secret-token"))
    await svc.upload(item.id, "s3://bucket/harbor_datasets")
    failed = svc.get(item.id)
    assert not failed.uploaded_at and failed.error == "AccessDenied <secret>"
    assert "отправьте" in svc.ready(item.id)
    svc.runner.handlers.clear()
    svc.runner.on("aws")
    await svc.upload(item.id, "s3://bucket/harbor_datasets")
    done = svc.get(item.id)
    assert done.uploaded_at and not done.error and svc.ready(item.id) == ""
    assert done.s3_uri.endswith(f"/local/{item.id}/tasks/")
    mapping = yaml.safe_load((svc.overlay / "datasets" / f"{item.id}.yaml").read_text())
    assert mapping["runtime"]["task_s3_base"] == done.s3_uri
    assert svc.runner.calls[-1][0] == tuple(svc.upload_argv(done, "s3://bucket/other"))
    assert svc.runner.calls[-1][1]["env"]["AWS_SECRET_ACCESS_KEY"] == "secret-token"
    (Path(item.path) / "one/environment/data.xlsx").write_text("mutated snapshot")
    assert "изменилась" in svc.ready(item.id)
    with pytest.raises(ValueError, match="изменилась"):
        svc.upload(item.id, "s3://bucket/harbor_datasets")


async def test_missing_write_credentials_never_calls_aws(tmp_path: Path, monkeypatch) -> None:
    svc = service(tmp_path)
    task(tmp_path / "source")
    item = svc.import_folder(tmp_path / "source", "sheet")
    monkeypatch.setattr(local_module, "secrets_env", lambda *args: {})
    await svc.upload(item.id, "s3://bucket/harbor_datasets")
    assert "PutObject" in svc.get(item.id).error and not svc.runner.calls


async def test_repeated_upload_errors_are_short_and_full_log_is_redacted(tmp_path: Path, monkeypatch) -> None:
    svc = service(tmp_path)
    task(tmp_path / "source")
    item = svc.import_folder(tmp_path / "source", "sheet")
    monkeypatch.setattr(local_module, "secrets_env", lambda *args: {
        "AWS_ACCESS_KEY_ID_gcvmfleet": "secret-key", "AWS_SECRET_ACCESS_KEY_gcvmfleet": "secret-token",
    })
    error = "An error occurred (XAmzContentSHA256Mismatch) when calling the PutObject operation: checksum mismatch"
    svc.runner.on("aws", result=Result((), 1, "", f"upload failed: a {error}\nupload failed: b {error}\nsecret-token"))
    await svc.upload(item.id, "s3://bucket/harbor_datasets")
    assert svc.get(item.id).error == error
    log = (svc.paths.logs / "datasets" / f"{item.id}.upload.log").read_text()
    assert "upload failed: a" in log and "upload failed: b" in log
    assert "secret-token" not in log and "<secret>" in log


async def test_preflight_checks_remote_task_and_redacts_errors(tmp_path: Path, monkeypatch) -> None:
    svc = service(tmp_path)
    task(tmp_path / "source")
    item = svc.import_folder(tmp_path / "source", "sheet")
    item.s3_uri, item.uploaded_at = "s3://bucket/harbor/local/tasks/", "now"
    svc.save(item)
    svc.settings.s3_read_profile = "read"
    monkeypatch.setattr(local_module, "secrets_env", lambda *args: {
        "AWS_ACCESS_KEY_ID_read": "read-key", "AWS_SECRET_ACCESS_KEY_read": "read-token",
    })
    svc.runner.on("aws", returncode=1)
    assert "в S3 нет" in await svc.remote_ready(item.id)
    svc.runner.handlers.clear()
    svc.runner.on("aws", stdout="2026-10-30 12:00:00 123 task.toml")
    assert await svc.remote_ready(item.id) == ""
    assert svc.runner.calls[-1][0][-1] == "s3://bucket/harbor/local/tasks/one/task.toml"
    assert svc.runner.calls[-1][1]["env"]["AWS_SECRET_ACCESS_KEY"] == "read-token"
    svc.runner.handlers.clear()
    svc.runner.on("aws", result=Result((), 1, "", "AccessDenied read-token"))
    assert await svc.remote_ready(item.id) == "AccessDenied <secret>"


async def test_concurrent_upload_and_bad_destination(tmp_path: Path, monkeypatch) -> None:
    svc = service(tmp_path)
    task(tmp_path / "source")
    item = svc.import_folder(tmp_path / "source", "sheet")
    for bad in ("", "https://host/path", "s3://bucket/${unknown}", "s3://bucket/a/../b"):
        with pytest.raises(ValueError, match="S3-префикс"):
            svc.upload_argv(item, bad)
    running = svc.upload(item.id, "s3://bucket/datasets")
    with pytest.raises(ValueError, match="уже отправляется"):
        svc.upload(item.id, "s3://bucket/datasets")
    await running


def test_no_root_and_failed_import_leave_no_dataset(tmp_path: Path) -> None:
    svc = service(tmp_path)
    task(tmp_path / "source")
    svc.settings.datasets_root = ""
    with pytest.raises(ValueError, match="корень"):
        svc.import_folder(tmp_path / "source", "sheet")
    assert not svc.all()


def test_failed_snapshot_copy_is_not_registered(tmp_path: Path, monkeypatch) -> None:
    svc = service(tmp_path)
    original = task(tmp_path / "source")

    def fail_copy(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(local_module.shutil, "copytree", fail_copy)
    with pytest.raises(OSError, match="disk full"):
        svc.import_folder(original.parent, "sheet")
    assert not svc.all() and not list((tmp_path / "data/local").iterdir())
    assert not list(svc.overlay.glob("datasets/*.yaml"))
    assert (original / "environment/data.xlsx").read_text() == "example"


def test_imported_mapping_uses_same_absolute_s3_prefix_for_fetch(tmp_path: Path) -> None:
    from fleet_console.compose.datasets import plan_fetch

    svc = service(tmp_path)
    task(tmp_path / "source")
    item = svc.import_folder(tmp_path / "source", "sheet")
    item.s3_uri = "s3://bucket/harbor/local/tasks/"
    svc.mapping(item)
    mappings, errors = build_registry(tmp_path / "wt", svc.overlay, tmp_path / "run")
    assert not errors
    fetched = plan_fetch(mappings[0], tmp_path / "data", "s3://other/datasets")
    assert fetched.src == item.s3_uri and fetched.dst == Path(item.path)
