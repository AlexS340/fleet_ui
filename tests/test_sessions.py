"""Сессии моделей на FakeRunner: стадии fleet, таймер, гашение, возобновление — без кластера и fleet."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fleet_console.fleetref.git import CommitInfo
from fleet_console.fleetref.manager import ReadyRevision
from fleet_console.fleetref.profile import CONFIGS_REL, Profile
from fleet_console.models import SessionError, SessionManager
from fleet_console.models.compose import SessionSpec, gpu_in, make_run_id, recipe_summary
from fleet_console.runner import FakeRunner, Result
from fleet_console.settings import Paths, Settings
from fleet_console.store import Store

SHA = "378874ba717f0fc37c2df6551351fa4d4e549eee"
T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _override(argv: tuple[str, ...], key: str) -> str | None:
    for a, b in zip(argv, argv[1:]):
        if a == "-o" and b.startswith(key + "="):
            return b.split("=", 1)[1]
    return None


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.now = T0
        self.paths = Paths(tmp_path / "home").ensure()
        self.store = Store(self.paths.db)
        self.runner = FakeRunner()
        self.steps: list[str] = []
        self.fail: set[str] = set()
        worktree = tmp_path / "wt"
        recipe = worktree / CONFIGS_REL / "mlsc" / "model" / "glm.yaml"
        recipe.parent.mkdir(parents=True)
        recipe.write_text(
            "# @package mlsc\ndefaults:\n  - vllm_base@_here_\n  - _self_\nserved_model_name: GLM-5.3-Flash\n", encoding="utf-8"
        )
        (recipe.parent / "vllm_base.yaml").write_text(  # тип инстанса приходит из базового рецепта, как в fleet
            "create_job:\n  job:\n    instance_type_name: a100plus.8gpu.80vG.96C.1456G\n    image: fleet-vllm:0.24.0\n", encoding="utf-8"
        )
        profile = Profile(sha=SHA, layout="harbor_new", groups={"mlsc/model": ["glm"], "harbor/agent": ["openhands"], "harbor/dataset": ["cowork"]})
        self.store.upsert_revision(SHA, profile.to_dict(), "master")
        self.revision = ReadyRevision("master", CommitInfo(SHA, T0, "egorbu", "subject", True, 0), worktree, profile, False)

        def fleet(argv: tuple[str, ...]) -> Result:
            step = "preflight" if "--print-config" in argv else _override(argv, "main.run").strip("[]").split(".")[-1]
            step = {"create_job": "create_job", "start_llm": "start_llm", "check_llm_works": "check", "stop_llm": "stop"}.get(step, step)
            self.steps.append(step)
            if step in self.fail:
                return Result(argv, 1, f"{step} упал", "")  # как настоящий stream: stderr слит в stdout и в лог
            if step == "create_job":
                Path(_override(argv, "mlsc.version_id_file")).write_text("1234\n", encoding="utf-8")
            return Result(argv, 0, "mlsc:\n  served_model_name: GLM-5.3-Flash\n" if step == "preflight" else f"{step} ok\n")

        self.runner.on("uv", "run", result=fleet)
        self.runner.on("osascript")

        async def pod() -> str:
            return "litellm-noauth-abc"

        settings = Settings()
        settings.session_grace_minutes = 10
        self.mgr = SessionManager(self.store, self.runner, self.paths, settings, token=lambda: "tok", mlsc_pod=pod, clock=lambda: self.now, platform="darwin")

    async def start(self, **kw):
        session = await self.mgr.start(self.revision, recipe="glm", model_name="GLM-5.3-Flash", replicas=2, **kw)
        await self.mgr.tasks[session.id]
        return self.mgr.get(session.id)

    def commands(self, session_id: str) -> dict[str, str]:
        return {s.stage: s.command for s in self.store.stages(session_id)}


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_compose_helpers(tmp_path: Path, h: Harness) -> None:
    assert make_run_id("GLM-5.3-Flash", T0) == "console-glm-5-3-flash-20261021-120000"
    assert gpu_in("a100plus.8gpu.80vG.96C.1456G") == 8 and gpu_in("${mlsc.instance_types.gpu1}") == 1 and gpu_in("cpu.48C") is None
    summary = recipe_summary(h.revision.worktree, "glm")
    assert summary["served_model_name"] == "GLM-5.3-Flash" and summary["gpu_per_instance"] == 8 and summary["image"] == "fleet-vllm:0.24.0"
    assert recipe_summary(h.revision.worktree, "missing") == {}
    spec = SessionSpec("r1", Path("/wt"), Path("/runs/r1"), "glm", 2, "nginx", "a", "d", "pod-x", "GLM", ("experiment.agents_per_replica=5",))
    argv = spec.argv("stop", jobs=0)
    assert argv[:6] == ["uv", "run", "--project", "/wt", "--no-sync", "fleet"] and argv[6] == "/runs/r1/console_model.yaml"
    ov = spec.overrides("check")
    assert ov[0] == "hydra.searchpath=[file:///wt/examples/harbor_new/configs]" and ov[-1] == "main.run=[mlsc.check_llm_works]"
    assert "mlsc.create_job.job.application_name='fleet-console r1'" in ov and "mlsc.served_model_name=GLM" in ov
    assert "experiment.jobs=0" in spec.overrides("stop", jobs=0) and "experiment.agents_per_replica=5" in ov
    assert "--print-config" in spec.argv("create_job", print_config=True)


async def test_session_comes_up_through_fleet_stages(h: Harness) -> None:
    session = await h.start(ttl_minutes=60)
    assert session.status == "ready" and session.version_id == 1234 and session.stage == ""
    assert session.expires_at == (T0 + timedelta(minutes=60)).isoformat(timespec="seconds")
    assert h.steps == ["preflight", "create_job", "start_llm", "check"]
    cmds = h.commands(session.id)
    assert "main.run=[mlsc.create_job]" in cmds["create_job"] and "main.run=[mlsc.check_llm_works]" in cmds["check"]
    assert "experiment.jobs=2" in cmds["start_llm"] and "--print-config" in cmds["preflight"] and "tok" not in cmds["create_job"]
    run_dir = Path(session.dir)
    assert (run_dir / "console_model.yaml").exists() and (run_dir / "resolved.yaml").read_text().startswith("mlsc:")
    assert (run_dir / "version_id").read_text().strip() == "1234" and (run_dir / "logs" / "check.log").exists()
    envs = [meta["env"] for argv, meta in h.runner.calls if argv[:2] == ("uv", "run")]
    assert envs[0]["MLSC_TOKEN"] == "fleet-console-dummy" and "FLEET_DATASETS_ROOT" in envs[0]  # preflight: только заглушки, токен в resolved.yaml не попадает
    assert envs[1] == {"MLSC_TOKEN": "tok"}  # стадии: только токен, остальное из окружения сервиса
    assert [s.returncode for s in h.store.stages(session.id)] == [0, 0, 0, 0]
    assert [e.action for e in h.store.events()][:2] == ["session_ready", "session_start"]


async def test_failures_before_and_after_gpu(h: Harness) -> None:
    h.fail = {"preflight"}
    session = await h.start()
    assert session.status == "failed" and "preflight" in session.error and h.steps == ["preflight"]
    h.fail = {"check"}
    session = await h.start()
    assert session.status == "attention" and session.version_id == 1234 and "check" in session.error and "упал" in session.error
    with pytest.raises(SessionError):
        await h.mgr.start(h.revision, recipe="nope")
    h.mgr.token = lambda: None
    with pytest.raises(SessionError):
        await h.mgr.start(h.revision, recipe="glm")


async def test_stop_replicas_extend(h: Harness) -> None:
    session = await h.start()
    h.steps.clear()
    session = await h.mgr.set_replicas(session.id, 3)
    assert session.replicas == 3 and h.steps == ["start_llm"] and "experiment.jobs=3" in h.store.stages(session.id)[-1].command
    before = session.expires_at
    session = await h.mgr.extend(session.id, 30)
    assert datetime.fromisoformat(session.expires_at) - datetime.fromisoformat(before) == timedelta(minutes=30)
    session = await h.mgr.stop(session.id)
    assert session.status == "stopped" and session.stopped_at and h.steps[-1] == "stop"
    assert "main.run=[mlsc.stop_llm]" in h.store.stages(session.id)[-1].command
    assert h.mgr.sessions(active_only=True) == [] and len(h.mgr.sessions()) == 1


async def test_timer_warns_then_stops_after_grace(h: Harness) -> None:
    session = await h.start(ttl_minutes=60)
    h.now = T0 + timedelta(minutes=59)
    await h.mgr.tick()
    assert h.mgr.get(session.id).warned == 0
    h.now = T0 + timedelta(minutes=61)
    await h.mgr.tick()
    s = h.mgr.get(session.id)
    assert s.warned == 1 and s.status == "ready"
    notified = [argv for argv, _ in h.runner.calls if argv[0] == "osascript"]
    assert len(notified) == 1 and "погашу через 10 мин" in notified[0][-1]
    await h.mgr.tick()  # повторного уведомления нет, гасить рано
    assert len([a for a, _ in h.runner.calls if a[0] == "osascript"]) == 1 and h.steps[-1] == "check"
    h.now = T0 + timedelta(minutes=72)
    await h.mgr.tick()
    assert h.mgr.get(session.id).status == "stopped" and h.steps[-1] == "stop"
    assert any(e.details == "истекло время жизни" for e in h.store.events())


async def test_resume_after_restart(h: Harness) -> None:
    ready = await h.start()
    h.store.update_session(ready.id, status="starting", stage="check")  # сервис перезапущен посреди check
    no_version = await h.start()
    h.store.update_session(no_version.id, status="starting", version_id=None)
    stopping = await h.start()
    h.store.update_session(stopping.id, status="stopping")
    h.steps.clear()
    await h.mgr.resume()
    await asyncio.gather(*h.mgr.tasks.values())
    assert h.mgr.get(ready.id).status == "ready" and h.steps == ["start_llm", "check"]  # без повторного create_job
    assert h.mgr.get(no_version.id).status == "failed" and "version_id" in h.mgr.get(no_version.id).error
    assert h.mgr.get(stopping.id).status == "attention"


async def test_start_without_model_name_takes_served_model_name_from_recipe(h: Harness) -> None:
    session = await h.mgr.start(h.revision, recipe="glm", model_name="", replicas=1)
    await h.mgr.tasks[session.id]
    assert h.mgr.get(session.id).model == "GLM-5.3-Flash"
    assert session.id.startswith("console-glm-")  # id по рецепту, как и раньше
    assert "mlsc.served_model_name=" not in h.commands(session.id)["create_job"]  # имя из рецепта не переопределяется


async def test_session_under_bench_is_paused_and_protected(h: Harness) -> None:
    session = await h.start(ttl_minutes=60)
    h.mgr.paused = lambda: {session.id}
    h.now = T0 + timedelta(minutes=61)
    await h.mgr.tick()
    s = h.mgr.get(session.id)
    assert s.status == "ready" and s.warned == 0 and datetime.fromisoformat(s.expires_at) == h.now + timedelta(minutes=10)  # срок сдвинут
    with pytest.raises(SessionError, match="идёт бенч"):
        await h.mgr.stop(session.id)
    assert (await h.mgr.stop(session.id, force=True)).status == "stopped"
