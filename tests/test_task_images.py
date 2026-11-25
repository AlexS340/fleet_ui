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
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(registry)) as client:
        assert task_images._probe(client, IMAGE + suffix) is None


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
        for image in ["busybox", "org/image", "registry.mlrnd.ru.evil/image", "127.0.0.1:80/image"]:
            issue = task_images._probe(client, image)
            assert not issue.blocking and not issue.url
        for path in ["../foo", "foo?bar", "repo@sha256:abc", "repo:"]:
            assert task_images._probe(client, "registry.mlrnd.ru/" + path).blocking


def image_task(source: Path, name: str):
    toml = task(source, name) / "task.toml"
    toml.write_text(toml.read_text().replace("registry.example/sheet:v2", IMAGE + ":v2"))


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
    with pytest.raises(ImportValidationError, match="one, two") as exc:
        svc.import_folder(source, "missing")
    assert calls == [IMAGE + ":v2"] and "/manifests/v2" in str(exc.value)
    assert not svc.all() and not list(svc.overlay.glob("datasets/*.yaml"))
    monkeypatch.setattr(task_images, "_probe", lambda *_: task_images.ImageIssue("Нет доступа"))
    assert validate_import(source).ok
    assert "Нет доступа" in svc.import_folder(source, "offline").warnings[0]


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
    await user.should_see("sales-review")
    assert any(link.props.get("href") == url for link in user.find(kind=ui.link).elements)
    assert not user.find(marker="local-import").elements.pop().enabled
    monkeypatch.setattr(task_images, "_probe", lambda *_: None)
    user.find(marker="local-check").click()
    await user.should_see("Минимальная проверка пройдена")
    await user.should_not_see("Образ отсутствует")
    monkeypatch.setattr(task_images, "_probe", lambda *_: missing)
    user.find(marker="local-import").click()
    await user.should_see("Образ отсутствует")
    assert any(link.props.get("href") == url for link in user.find(kind=ui.link).elements)
    assert not user.find(marker="local-import").elements.pop().enabled
