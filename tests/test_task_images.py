import json
from pathlib import Path

import httpx
import pytest
from nicegui import ui
from nicegui.testing import User
from test_local_datasets import service, task

from fleet_console.compose import task_images
from fleet_console.compose.local_datasets import ImportValidationError, validate_import

REPO = "docker-mlrnd/sheet-taskgen/image"
IMAGE = f"registry.mlrnd.ru/{REPO}"
DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(("suffix", "reference"), [
    (":v2", "v2"), (f"@{DIGEST}", DIGEST), (f":v2@{DIGEST}", DIGEST), ("", "latest"),
])
def test_harbor_anonymous_manifest_check(suffix, reference):
    def registry(request):
        assert request.url.host == "registry.mlrnd.ru"
        if request.method == "GET":
            assert request.url.path == "/service/token"
            assert request.url.params["scope"] == f"repository:{REPO}:pull"
            return httpx.Response(200, json={"token": "anonymous-token"})
        assert request.method == "HEAD" and request.url.path == f"/v2/{REPO}/manifests/{reference}"
        assert "application/vnd.oci.image.index.v1+json" in request.headers["accept"]
        if "authorization" not in request.headers:
            return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer realm="https://registry.mlrnd.ru/service/token"'})
        assert request.headers["authorization"] == "Bearer anonymous-token"
        return httpx.Response(200, headers={"Docker-Content-Digest": DIGEST})

    with httpx.Client(transport=httpx.MockTransport(registry)) as client:
        assert task_images._probe(client, IMAGE + suffix) == task_images.Manifest(
            f"https://registry.mlrnd.ru/v2/{REPO}/manifests/{reference}", DIGEST,
        )


@pytest.mark.parametrize("status", [401, 403, 404, 429, 502, 302])
def test_only_missing_manifest_blocks_import(status):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status))) as client:
        issue = task_images._probe(client, IMAGE + ":v2")
    assert issue.blocking == (status == 404)
    assert issue.url == f"https://registry.mlrnd.ru/v2/{REPO}/manifests/v2"
    assert ("отсутствует" in issue.message) == (status == 404)


def test_token_service_failure_and_network_failure_are_inconclusive():
    def token_failure(request):
        if request.method == "HEAD":
            return httpx.Response(401, headers={"WWW-Authenticate": "Bearer challenge"})
        return httpx.Response(404)

    def timeout(request):
        raise httpx.ConnectTimeout("timed out")

    for handler in [token_failure, timeout]:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            issue = task_images._probe(client, IMAGE + ":v2")
            assert not issue.blocking and "Не удалось" in issue.message


def test_external_and_invalid_references_make_no_requests():
    def unexpected(request):
        pytest.fail(f"unexpected request to {request.url}")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        for image in ["busybox", "image:tag", "org/image", "docker.io/org/image", "registry.mlrnd.ru.evil/image",
                      "127.0.0.1:80/image", "registry.mlrnd.ru:8080/image"]:
            issue = task_images._probe(client, image)
            assert issue.blocking and not issue.url
        for path in ["../foo", "foo?bar", "repo@sha256:abc", "repo:"]:
            assert task_images._probe(client, "registry.mlrnd.ru/" + path).blocking


def image_task(source: Path, name: str):
    task(source, name, IMAGE + ":v2")


@pytest.mark.parametrize(("suffix", "mutable"), [(":v2", True), ("", True), (f":v2@{DIGEST}", False)])
@pytest.mark.parametrize("header", [DIGEST, ""])
def test_import_records_manifest_observed_at_save(tmp_path, monkeypatch, suffix, mutable, header):
    source = tmp_path / "source"
    image = IMAGE + suffix
    task(source, "one", image)
    task(source, "two", image)
    preview_digest = "sha256:" + "b" * 64 if mutable else DIGEST
    headers = iter([{"Docker-Content-Digest": preview_digest}, {"Docker-Content-Digest": header} if header else {}])

    def head(client, url, **kwargs):
        return httpx.Response(200, headers=next(headers), request=httpx.Request("HEAD", url))

    monkeypatch.setattr(httpx.Client, "head", head)
    preview = validate_import(source)
    assert preview.ok and preview.image_digests == {image: preview_digest}
    svc = service(tmp_path)
    item = svc.import_folder(source, "snapshot")
    saved = service(tmp_path).get(item.id)
    assert saved.image_digests == ({image: header} if header else {})
    assert saved.digest == preview.digest
    assert (Path(saved.path) / "one/task.toml").read_bytes() == (source / "one/task.toml").read_bytes()
    assert any("Тег может измениться" in w and "one" in w and "two" in w for w in saved.warnings) == mutable
    assert any("не вернул Docker-Content-Digest" in w for w in saved.warnings) == (not header)


def test_read_dataset_record_without_image_digests(tmp_path):
    svc = service(tmp_path)
    record = {"id": "local-old", "name": "old", "source": "src", "path": "dst", "imported_at": "then",
              "tasks": ["one"], "size": 42, "warnings": [], "digest": "f" * 64}
    svc.directory.mkdir(parents=True, exist_ok=True)
    (svc.directory / "local-old.json").write_text(json.dumps(record))
    old = svc.get("local-old")
    assert old.image_digests == {} and old.digest == record["digest"] and svc.all() == [old]


async def test_remove_task_keeps_import_digests_for_remaining_references(tmp_path, monkeypatch):
    source = tmp_path / "source"
    kept = IMAGE + ":keep"
    task(source, "one", kept)
    task(source, "two", IMAGE + ":remove")
    monkeypatch.setattr(task_images, "_probe", lambda _, image: task_images.Manifest(image, DIGEST))
    svc = service(tmp_path)
    item = svc.import_folder(source, "snapshot")
    assert len(item.image_digests) == 2
    monkeypatch.setattr(task_images, "_probe", lambda *_: pytest.fail("unexpected registry request"))
    await svc.remove_task(item.id, "two", owner_id=item.owner_id)
    assert svc.get(item.id).image_digests == {kept: DIGEST}


def test_duplicate_missing_image_blocks_whole_snapshot(tmp_path, monkeypatch):
    source = tmp_path / "source"
    for name in ["one", "two"]:
        image_task(source, name)
    calls = []

    def missing(client, image):
        calls.append(image)
        return task_images.ImageIssue("Образ отсутствует в реестре", f"https://registry.mlrnd.ru/v2/{REPO}/manifests/v2", True)

    monkeypatch.setattr(task_images, "_probe", missing)
    svc = service(tmp_path)
    with pytest.raises(ImportValidationError) as exc:
        svc.import_folder(source, "missing")
    for name in ("one", "two"):
        assert f"{name}: environment.docker_image" in str(exc.value) and f"{name}: tests/Dockerfile:1" in str(exc.value)
    assert calls == [IMAGE + ":v2"] and "/manifests/v2" in str(exc.value)
    assert not svc.all() and not list(svc.overlay.glob("datasets/*.yaml"))
    monkeypatch.setattr(task_images, "_probe", lambda *_: task_images.ImageIssue("Нет доступа"))
    assert validate_import(source).ok
    assert any("Нет доступа" in warning for warning in svc.import_folder(source, "offline").warnings)


@pytest.mark.nicegui_main_file("tests/ui_main.py")
async def test_import_dialog_links_missing_image_at_validation_and_save(user: User, tmp_path, monkeypatch):
    image_task(tmp_path, "sales-review")
    url = f"https://registry.mlrnd.ru/v2/{REPO}/manifests/v2"
    missing = task_images.ImageIssue("Образ отсутствует в реестре", url, True)
    monkeypatch.setattr(task_images, "_probe", lambda *_: missing)
    await user.open("/runs")
    user.find(marker="local-add").click()
    await user.should_see("Добавить локальные задачи")
    user.find(marker="local-folder").elements.pop().set_value(str(tmp_path))
    user.find(marker="local-check").click()
    await user.should_see("Образ отсутствует")
    await user.should_see("sales-review: tests/Dockerfile:1")
    assert any(link.props.get("href") == url for link in user.find(kind=ui.link).elements)
    assert not user.find(marker="local-import").elements.pop().enabled
    monkeypatch.setattr(task_images, "_probe", lambda *_: task_images.Manifest(url, DIGEST))
    user.find(marker="local-check").click()
    await user.should_see("Минимальная проверка пройдена")
    await user.should_see("Тег может измениться")
    await user.should_not_see("Образ отсутствует")
    monkeypatch.setattr(task_images, "_probe", lambda *_: missing)
    user.find(marker="local-import").click()
    await user.should_see("Образ отсутствует")
    assert any(link.props.get("href") == url for link in user.find(kind=ui.link).elements)
    assert not user.find(marker="local-import").elements.pop().enabled
