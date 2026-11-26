import httpx
import pytest
from test_local_datasets import service, task

from fleet_console.compose.local_datasets import ImportValidationError, validate_import

MAIN = "registry.mlrnd.ru/tasks/main:v1"


@pytest.mark.parametrize(("main", "verifier", "dockerfile", "location"), [
    ("image:tag", "", "FROM scratch", "environment.docker_image"),
    (MAIN, 'docker_image = "busybox"', "FROM scratch", "verifier.environment.docker_image"),
    (MAIN, "", "ARG BASE=docker.io/team/tool\nFROM ${BASE}", "tests/Dockerfile:2"),
    (MAIN, "", "FROM scratch AS builder\nFROM registry.mlrnd.ru.evil/img", "tests/Dockerfile:2"),
])
def test_foreign_image_blocks_before_any_request(tmp_path, monkeypatch, main, verifier, dockerfile, location):
    path = task(tmp_path / "source", "review", main)
    with (path / "task.toml").open("a") as stream:
        stream.write(verifier)
    (path / "tests/Dockerfile").write_text(dockerfile)
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **kw: pytest.fail("unexpected HTTP"))
    svc = service(tmp_path)
    with pytest.raises(ImportValidationError) as exc:
        svc.import_folder(path, "review")
    assert f"review: {location}" in str(exc.value)
    assert not svc.all()


@pytest.mark.parametrize("verifier", [
    '[verifier]\nenvironment_mode = "shared"',
    '[verifier]\nenvironment_mode = "separate"',  # inherits the main image
    '[verifier.environment]\ndocker_image = "registry.mlrnd.ru/tasks/verifier:v2"',
])
def test_unused_verifier_dockerfile_is_ignored(tmp_path, verifier):
    path = task(tmp_path, image=MAIN)
    (path / "task.toml").write_text(f'[environment]\ndocker_image = "{MAIN}"\n{verifier}')
    (path / "tests/Dockerfile").write_text("FROM busybox")
    report = validate_import(path)
    assert report.ok
    assert set(report.images) == ({MAIN, "registry.mlrnd.ru/tasks/verifier:v2"} if "docker_image" in verifier else {MAIN})


def test_multistage_verifier_build_and_global_args(tmp_path, monkeypatch):
    image = "registry.mlrnd.ru:1443/tools/base:one"
    path = task(tmp_path, image=image)
    (path / "task.toml").write_text(f'[environment]\ndocker_image = "{image}"\n[verifier.environment]\n')
    (path / "tests/Dockerfile").write_text(
        'ARG REG="registry.mlrnd.ru:1443"\n'
        'ARG BASE=${REG}/tools/base:one\n'
        'FROM --platform=$BUILDPLATFORM \\\n'
        '    $BASE AS Builder\n'
        'RUN <<EOF\nFROM ignored\nEOF\n'
        'from builder as final\n'
        'FROM scratch AS empty\n'
        'ARG DYNAMIC=busybox\n'  # stage-local, invisible to FROM
        'FROM ${DYNAMIC} AS dynamic\n'
        'FROM dynamic\n'
        'FROM registry.mlrnd.ru:443/verify/final\n'
    )
    requests = []

    def registry(transport, request):
        requests.append((request.method, request.url.port, request.url.path))
        return httpx.Response(403)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", registry)
    svc = service(tmp_path)
    item = svc.import_folder(path, "multistage")
    assert sorted(requests, key=lambda request: request[2]) == [
        ("HEAD", 1443, "/v2/tools/base/manifests/one"), ("HEAD", None, "/v2/verify/final/manifests/latest"),
    ]
    assert any("one: tests/Dockerfile:11" in w and "${DYNAMIC}" in w and "ARG" in w for w in item.warnings)
    assert any("one: environment.docker_image" in w and "one: tests/Dockerfile:3" in w and "403" in w for w in item.warnings)
    assert any("one: tests/Dockerfile:13" in w and "403" in w for w in item.warnings)


def test_missing_verifier_manifest_lists_both_sources(tmp_path, monkeypatch):
    source = tmp_path / "source"
    verifier = "registry.mlrnd.ru/tasks/verify:v2"
    ready = task(source, "prebuilt", MAIN)
    with (ready / "task.toml").open("a") as stream:
        stream.write(f'docker_image = "{verifier}"\n')
    building = task(source, "build", MAIN)
    (building / "tests/Dockerfile").write_text(f"FROM scratch AS files\nFROM {verifier}\n")
    requests = []

    def registry(transport, request):
        requests.append((request.method, request.url.path))
        return httpx.Response(404 if request.url.path.endswith("/v2") else 200)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", registry)
    svc = service(tmp_path)
    with pytest.raises(ImportValidationError) as exc:
        svc.import_folder(source, "missing")
    assert sorted(requests) == [("HEAD", "/v2/tasks/main/manifests/v1"), ("HEAD", "/v2/tasks/verify/manifests/v2")]
    assert "prebuilt: verifier.environment.docker_image" in str(exc.value)
    assert "build: tests/Dockerfile:2" in str(exc.value)
    assert "prebuilt: environment.docker_image" not in str(exc.value)
    assert not svc.all()
