import base64
import json
from pathlib import Path

import httpx
import pytest
from test_benches import Harness
from test_compose import spec_for

from fleet_console.compose import Preview, load_issues, task_issues
from fleet_console.compose.local_datasets import validate_folder
from fleet_console.compose.task_compatibility import compose_conflicts
from fleet_console.compose.verifier_image import _inspect
from fleet_console.runner import FakeRunner, Result
from fleet_console.runs.manager import BenchError

OLD = "registry.mlrnd.ru/docker-mlrnd/gc-cli/gc-cli-harbor:v0.3.0_260910"
FIXED = "registry.mlrnd.ru/docker-mlrnd/sheet-taskgen/harbor-runner:buildx-20261102"


@pytest.mark.parametrize(("policy", "expected"), [
    ("allow_internet=false", "no_network"),
    ('network_mode="allowlist"\nallowed_hosts=["example.org"]', "allowlist"),
    ('network_mode="public"\nallow_internet=false', "public"),
])
def test_import_features(tmp_path, policy, expected):
    (tmp_path / "task.toml").write_text(
        f'[environment]\n{policy}\ndocker_image="main"\n'
        '[[environment.mcp_servers]]\nname="tools"\n[verifier.environment]\ndocker_image="verifier"'
    )
    facts = validate_folder(tmp_path).features[tmp_path.name]
    assert facts.network_mode == expected
    assert facts.allowed_hosts == (("example.org",) if expected == "allowlist" else ())
    assert facts.has_mcp and facts.verifier_mode == "separate" and facts.verifier_image == "verifier"


def test_egress_scope_and_compose_service(tmp_path):
    overlay = {"services": {"main": {"extra_hosts": ["mirror:1.2.3.4"]}}}
    assert compose_conflicts(tmp_path, [overlay]) == ["main"]
    (tmp_path / "docker-compose.yaml").write_text("services: {main: {networks: [app]}, db: {network_mode: 'service:main'}}")
    assert compose_conflicts(tmp_path, [overlay]) == []
    overlay["services"]["db"] = {"extra_hosts": ["mirror:1.2.3.4"]}
    assert compose_conflicts(tmp_path, [overlay]) == ["db"]


@pytest.mark.parametrize("mode", ["no_network", "allowlist"])
def test_network_matrix_and_mcp(mode):
    task = {"path": "/data/a", "agent": "terminus-2", "egress": [mode], "mcp": True}
    plain = {"path": "/data/b", "agent": "terminus-2"}
    other = {"path": "/data/c", "agent": "opencode", "mcp": True}

    def findings(image, conflicts):
        tasks = [{**task, "compose_conflict": conflicts}, plain, other]
        checks = task_issues.match_tasks(load_issues(), tasks, image)
        assert all(c.detail.endswith("Задачи: a") for c in checks)
        return {c.key.removeprefix("issue:"): c.level for c in checks}

    mcp = {"terminus2-mcp-harbor-0.22": "warn"}
    assert findings(OLD, ["main"]) == {**mcp, "egress-extra-hosts": "fail", "egress-buildx": "fail"}
    assert findings(OLD, []) == {**mcp, "egress-buildx": "fail"}
    assert findings(FIXED, ["main"]) == {**mcp, "egress-extra-hosts": "fail"}
    assert findings(FIXED, []) == mcp
    assert findings(OLD + "@sha256:" + "a" * 64, []) == {**mcp, "egress-buildx": "warn"}


@pytest.mark.parametrize(("code", "message", "level"), [
    (200, "", "warn"),
    (404, "Could not find the file /tests/test.sh in container preflight", "fail"),
    (404, "No such container", "warn"),
    (403, "Access denied", "warn"),
])
def test_verifier_checks_inside_image_once_and_cleans_up(monkeypatch, code, message, level):
    methods = []
    def engine(request):
        methods.append(request.method)
        if request.method == "POST":
            assert request.url.path == "/containers/create"
            return httpx.Response(201)
        if request.method == "GET":
            assert request.url.params["path"] == "/tests/test.sh"
            stat = base64.b64encode(b'{"mode":493}').decode()
            return httpx.Response(code, json={"message": message}, headers={"X-Docker-Container-Path-Stat": stat})
        assert request.method == "DELETE" and request.url.params["v"] == "true"
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(engine), base_url="http://localhost") as client:
        monkeypatch.setattr(task_issues, "script_present", lambda image: _inspect(client, image))
        checks = task_issues.match_tasks(load_issues(), [
            {"path": n, "agent": "oracle", "verifier_images": ["verifier"]} for n in ("a", "b")
        ], FIXED)
    assert methods == ["POST", "GET", "DELETE"]
    assert len(checks) == 1 and checks[0].level == level and checks[0].detail.endswith("Задачи: a, b")


def install_probe(runner, worktree, records):
    (worktree / "pyproject.toml").write_text('dependencies = ["harbor==0.22.0"]')
    argv = (str(worktree / ".venv/bin/python"), "-I", "-B", str(Path(task_issues.__file__).with_name("task_compatibility.py")))
    runner.handlers.insert(0, (argv, Result(argv, 0, json.dumps(records))))


async def test_effective_builder_overrides_and_harbor_version(tmp_path):
    spec = spec_for(tmp_path)
    runner = FakeRunner()
    install_probe(runner, spec.worktree, [{"path": "one", "agent": "terminus-2", "egress": ["allowlist"]}])
    system = {"materialize": {"job_source": {"config": {"datasets": [{"n_tasks": 1}]}}},
              "agent_image": OLD, "manifest_runner": {"workflow_builder": {"agent_image": FIXED}}}
    assert await task_issues.task_issue_checks(runner, spec, {"system": system}) == []
    assert json.loads(runner.calls[0][1]["stdin"]) == system["materialize"]
    assert runner.calls[0][1]["cwd"] == spec.worktree
    (spec.worktree / "pyproject.toml").write_text('dependencies = ["harbor==0.23.0"]')
    assert await task_issues.task_issue_checks(runner, spec, {"system": system}) == []
    assert len(runner.calls) == 1


async def test_confirmed_conflict_blocks_submit(tmp_path):
    harness = Harness(tmp_path)
    install_probe(harness.runner, harness.worktree, [{"path": "blocked-task", "agent": "oracle", "compose_conflict": ["main"]}])
    with pytest.raises(BenchError, match="Задачи: blocked-task"):
        await harness.mgr.start(harness.spec(), ref="master", preview=Preview(model="GLM"))
    assert not harness.store.benches() and "materialize" not in harness.stages


@pytest.mark.nicegui_main_file("tests/ui_main.py")
async def test_preflight_names_tasks_and_disables_start(user):
    import fakes
    ctx = fakes.CTX
    revision = await ctx.manager.prepare("master")
    install_probe(ctx.benches.runner, revision.worktree, [{"path": "with-network", "agent": "terminus-2", "compose_conflict": ["main"]}])
    await user.open("/runs")
    user.find("Запустить бенч").click()
    user.find("Превью и preflight").click()
    await user.should_see("Задачи: with-network")
    user.find(marker="accept-warnings").elements.pop().set_value(True)
    assert not user.find(marker="start-bench").elements.pop().enabled


async def test_probe_uses_selection_for_each_job(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from fleet_console.compose import task_compatibility as probe

    first = tmp_path / "explicit"
    second = tmp_path / "filtered"
    dataset = Mock()
    dataset.get_task_configs = AsyncMock(return_value=[SimpleNamespace(path=second)])
    job = SimpleNamespace(tasks=[SimpleNamespace(path=first)], datasets=[dataset], verifier=SimpleNamespace(disable=True))
    additional = SimpleNamespace(tasks=[SimpleNamespace(path=second)], datasets=[], verifier=SimpleNamespace(disable=False))
    source = Mock()
    source.model_validate.side_effect = [Mock(load=lambda: job), Mock(load=lambda: additional)]
    monkeypatch.setitem(sys.modules, "fleet.harbor", SimpleNamespace(HarborJobConfigSource=source))
    seen = []
    def features(config, selected_job, paths):
        seen.append((selected_job, paths))
        return []
    monkeypatch.setattr(probe, "job_features", features)
    await probe.selected_features({"job_source": {}, "additional_jobs": [{"job_source": {}}]})
    dataset.get_task_configs.assert_awaited_once_with(disable_verification=True)
    assert seen == [(job, [first, second]), (additional, [second])]
