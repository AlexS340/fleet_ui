import pytest

from fleet_console.control import active_workflows, kill_submitter, stop_run, terminate_workflows, write_agent_limit
from fleet_console.control.mlsc import set_planned_state
from fleet_console.runner import CommandError, FakeRunner, Result

NS = "swe-bench-v2-20260308"
POD = "fleet-submitter-python"
WF = "wf-1\tRunning\nwf-2\tSucceeded\nwf-3\t\nwf-4\tPending\n"


async def test_agent_limit_is_written_like_fleet() -> None:
    fake = FakeRunner()
    fake.on("kubectl", "exec")
    cmd = await write_agent_limit(fake, NS, POD, "run-a", 40)
    assert cmd.ok and cmd.argv[:5] == ("kubectl", "exec", "-n", NS, POD)
    assert "printf '%s' '40' > /manifests/harbor_run/run-a/agents_limit" in cmd.argv[-1] and "mkdir -p" in cmd.argv[-1]
    with pytest.raises(ValueError):
        await write_agent_limit(fake, NS, POD, "run-a", -1)
    fake.handlers.insert(0, (("kubectl", "exec"), Result(("x",), 1, "", "no such pod")))
    with pytest.raises(CommandError):
        await write_agent_limit(fake, NS, POD, "run-a", 0)


async def test_workflows_listing_termination_and_submitter_kill() -> None:
    fake = FakeRunner()
    fake.on("kubectl", "-n", NS, "get", "workflows", stdout=WF)
    fake.on("kubectl", "-n", NS, "patch", "workflow", "wf-3", returncode=1, stdout="", result=Result(("x",), 1, "", "not found"))
    fake.on("kubectl", "-n", NS, "patch", "workflow")
    fake.on("kubectl", "exec")
    names = await active_workflows(fake, NS, "run-a")
    assert names == ["wf-1", "wf-3", "wf-4"]  # без фазы = ещё не стартовал, тоже активный
    assert ("-l", "fleet/run-name=run-a") in zip(fake.calls[-1][0], fake.calls[-1][0][1:])
    commands = await terminate_workflows(fake, NS, names)
    assert [c.ok for c in commands] == [True, False, True] and '"shutdown":"Terminate"' in commands[0].argv[-1]
    assert "not found" in commands[1].output
    cmd = await kill_submitter(fake, NS, POD, "run-a")
    assert "pkill -f 'submit_workflows[.]py /manifests/harbor_run/run-a/'" in cmd.argv[-1]


async def test_stop_run_reports_each_step() -> None:
    fake = FakeRunner()
    fake.on("kubectl", "exec")
    fake.on("kubectl", "-n", NS, "patch", "workflow")
    listings = iter([WF, "wf-2\tSucceeded\n"])
    fake.on("kubectl", "-n", NS, "get", "workflows", result=lambda argv: Result(argv, 0, next(listings)))
    report = await stop_run(fake, NS, POD, "run-a")
    assert report.ok and report.terminated == 3 and report.remaining == 0 and not report.failed
    assert report.steps[0] == "лимит агентов = 0" and "сабмиттер" in report.steps[1] and "3 из 3" in report.steps[2]
    assert len(report.commands) == 5 and all(c.text.startswith("kubectl") for c in report.commands)

    broken = FakeRunner()
    broken.on("kubectl", "exec", result=Result(("x",), 1, "", "exec forbidden"))
    broken.on("kubectl", "-n", NS, "get", "workflows", result=Result(("x",), 1, "", "api down"))
    report = await stop_run(broken, NS, POD, "run-a")
    assert not report.ok and len(report.failed) == 3 and report.remaining is None


async def test_set_planned_state_sends_token_via_stdin() -> None:
    fake = FakeRunner()
    fake.on("kubectl", "exec", "-i", stdout='{"ok":true}')
    out = await set_planned_state(fake, "vllm-proxy", "litellm-noauth-abc", "https://mlsc/mlc/", "SECRET", 1142, 0)
    argv, meta = fake.calls[-1]
    assert out == '{"ok":true}' and meta["stdin"] == "SECRET" and not any("SECRET" in a for a in argv)
    script = argv[-1]
    assert "set_planned_state" in script and '"1142": 0' in script and "-X POST" in script and "$(cat)" in script
    with pytest.raises(ValueError):
        await set_planned_state(fake, "vllm-proxy", "p", "https://mlsc/mlc/", "t", 1, -1)
