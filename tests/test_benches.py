"""Бенч-прогоны на FakeRunner: стадии fleet, цикл исполнения, сбои, возобновление, остановка — без кластера и fleet."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest
import yaml

import cluster_samples
from fleet_console.compose import BenchSpec, Preview, SessionLink
from fleet_console.fleetref.profile import CONFIGS_REL
from fleet_console.models import SessionManager
from fleet_console.observe import ClusterObserver
from fleet_console.runner import FakeRunner, Result, StageFiles
from fleet_console.runs import BenchError, BenchManager, plan, position
from fleet_console.settings import Paths, Settings
from fleet_console.store import Session, Store

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
NS = "swe-bench-v2-20260308"
RESOLVED_YAML = (
    "system:\n  namespace: ns\n  s3_base_directory: s3://b/ns/GLM/x\n  pending_dir: /manifests/harbor_run/x/pending\n"
    "mlsc:\n  served_model_name: GLM\n  create_job:\n    job:\n      instance_type_name: a100plus.1gpu\n"
    "experiment:\n  run_id: x\n  model_name: GLM\n  jobs: 1\n  agents_per_replica: 2\n  inference: litellm\n"
    "harbor_job:\n  agents:\n    - name: terminus-2\n  datasets:\n    - path: /data/birdsql\nharbor_dataset:\n  benchmark: birdsql\nexecution: {}\n"
)


def _override(argv: tuple[str, ...], key: str) -> str | None:
    for a, b in reversed(list(zip(argv, argv[1:]))):
        if a == "-o" and b.startswith(key + "="):
            return b.split("=", 1)[1]
    return None


def _steps(argv: tuple[str, ...]) -> list[str]:
    raw = _override(argv, "main.run") or "[]"
    return [s for s in raw.strip("[]").split(",") if s]


class Harness:
    """FakeRunner отвечает за fleet и kubectl; сабмиттер и Argo — по сценарию, который меняют тесты."""

    def __init__(self, tmp_path: Path) -> None:
        self.now = T0
        self.configuration = json.loads(cluster_samples.CONFIGURATION)
        self.still_running = False
        self.paths = Paths(tmp_path / "home").ensure()
        self.store = Store(self.paths.db)
        self.runner = FakeRunner()
        self.stages: list[str] = []  # первый шаг каждой стадии, в порядке вызова
        self.fail: set[str] = set()
        self.submitter = "alive"  # alive | done | dead_with_queue | missing
        self.argo_active = 0
        self.limits: list[int] = []
        self.stopped: list[str] = []
        self.requeue_moves = False
        self.worktree = tmp_path / "wt"
        self.runner.on(str(self.worktree / ".venv/bin/python"), stdout="3\n")
        cfg = self.worktree / CONFIGS_REL
        for rel, text in {
            "mlsc/model/glm.yaml": "# @package mlsc\nserved_model_name: GLM\n",
            "harbor/agent/terminus2.yaml": "harbor_job: {}\n",
            "harbor/dataset/birdsql.yaml": "harbor_dataset:\n  benchmark: birdsql\n",
            "datasets/birdsql.yaml": "benchmark: birdsql\nsource:\n  tasks_root: birdsql/tasks\nruntime:\n  task_s3_base: birdsql/tasks\n",
        }.items():
            (cfg / rel).parent.mkdir(parents=True, exist_ok=True)
            (cfg / rel).write_text(text, encoding="utf-8")
        (self.worktree / "fleet").mkdir()
        (self.worktree / "fleet" / "measure_run.py").write_text("class MeasureRunRunner:\n    read_only: bool = False\n")
        self.overlay = tmp_path / "overlay"
        (self.overlay / "datasets").mkdir(parents=True)

        def fleet(argv: tuple[str, ...]) -> Result:
            if "--print-config" in argv:
                self.stages.append("preflight")
                failed = "preflight" in self.fail
                data = yaml.safe_load(RESOLVED_YAML)
                data["experiment"]["run_id"] = _override(argv, "experiment.run_id")
                data["experiment"]["model_name"] = _override(argv, "mlsc.served_model_name")
                model = data["experiment"]["model_name"]
                data["mlsc"]["served_model_name"] = model
                data["harbor_job"]["agents"][0]["model_name"] = _override(argv, "harbor_job.agents.0.model_name") or f"openai/{model}"
                data["system"]["polar"] = {"topology": {"gateway": {"nodes": [{"inference": {"base_url": "http://litellm.test/v1"}}]}}}
                data["experiment"]["jobs"] = int(_override(argv, "experiment.jobs") or 1)
                data["system"]["measure_run"] = {"read_only": True}
                data["system"]["s3_base_directory"] = _override(argv, "system.s3_base_directory") or f"s3://b/ns/{data['experiment']['model_name']}/{data['experiment']['run_id']}"
                return Result(argv, 1 if failed else 0, "" if failed else yaml.safe_dump(data, sort_keys=False), "Hydra: boom" if failed else "")
            steps = _steps(argv)
            stage = {
                "system.materialize": "materialize", "mlsc.create_job": "create_job", "mlsc.start_llm": "start_llm",
                "mlsc.check_llm_works": "check", "mlsc.create_agent_limit": "upload", "system.submitter_launch": "launch",
                "system.requeue_failed": "requeue", "mlsc.stop_llm": "stop_llm", "system.measure_run": "measure",
            }[steps[0]]
            self.stages.append(stage)
            if stage in self.fail:
                return Result(argv, 1, f"{stage} упал")
            if stage == "create_job":
                version_id = str(1234 + sum(int(k) >= 1234 for k in self.configuration["application_versions"]))
                Path(_override(argv, "mlsc.version_id_file")).write_text(version_id + "\n", encoding="utf-8")
                self.configuration["application_versions"][version_id] = {"application_name": f"fleet-console {_override(argv, 'experiment.run_id')}"}
            if stage == "stop_llm":
                version_id = Path(_override(argv, "mlsc.version_id_file")).read_text().strip()
                self.configuration["application_versions"].setdefault(version_id, {"application_name": "fleet-console test"})
                self.configuration["application_version_states"][version_id] = {"planned": 0, "current": 1 if self.still_running else 0}
            if stage == "materialize":
                out = Path(_override(argv, "output_dir")) / ".fleet"
                out.mkdir(parents=True, exist_ok=True)
                (out / "tasks.json").write_text("[{}, {}, {}]", encoding="utf-8")
            if stage == "requeue":
                return Result(argv, 0, "Перенесено submitted → pending: 2/2\n" if self.requeue_moves else "Submitted пуст — переносить нечего.\n")
            if stage == "measure" and _override(argv, "++system.measure_run.output_file"):
                Path(_override(argv, "++system.measure_run.output_file")).write_text(json.dumps({
                    "run_id": _override(argv, "experiment.run_id"), "sha": _override(argv, "++system.measure_run.sha"),
                    "results": [{"leaf": "birdsql/terminus-2", "score": 50, "planned": 3, "infra_errors": 1,
                                 "succeeded": 1, "timeouts": 0, "unattempted": 0, "reward_sum": 1.5}]}))
            return Result(argv, 0, f"{stage} ok\n")

        self.runner.on("uv", "run", result=fleet)
        self.runner.on("osascript")
        self.runner.on("kubectl", "exec", "-i", "-n", "vllm-proxy", result=lambda argv: Result(argv, 0, json.dumps(self.configuration)))
        cluster_samples.install(self.runner, NS)
        self.runner.handlers = [h for h in self.runner.handlers if h[0][:4] != ("kubectl", "exec", "-n", NS)]  # сабмиттер — по сценарию

        def submitter(argv: tuple[str, ...]) -> Result:
            if self.submitter == "missing":
                return Result(argv, 0, "")
            alive = 1 if self.submitter == "alive" else 0
            pending = 3 if self.submitter in ("alive", "dead_with_queue") else 0
            rows = [f"{r.id}\t{4 if alive else 0}\t{pending}\t5\t{2 if alive else 7}\t{alive}\t1761022800" for r in self.store.benches()]
            return Result(argv, 0, "\n".join(rows) + "\n")

        self.runner.on("kubectl", "exec", "-n", NS, "fleet-submitter-python", "--", "sh", result=submitter)
        self.runner.on("kubectl", "exec", "-n", NS, "fleet-submitter-python", "--", "tail", stdout="")

        def argo(argv: tuple[str, ...]) -> Result:
            rows = "".join(f"wf-x-{i}\tRunning\trun-x\tGLM\t2026-10-21T05:00:00Z\t\n" for i in range(self.argo_active))
            if "-l" in argv:
                return Result(argv, 0, rows if "fleet/run-name=run-x" in argv else "")
            return Result(argv, 0, rows + cluster_samples.ARGO_TEXT)

        self.runner.handlers = [h for h in self.runner.handlers if h[0][:5] != ("kubectl", "-n", NS, "get", "workflows")]
        self.runner.on("kubectl", "-n", NS, "get", "workflows", result=argo)

        async def pod() -> str:
            return "litellm-noauth-abc"

        async def limit(run_id: str, n: int) -> None:
            self.limits.append(n)

        async def stop_tasks(run_id: str):
            self.stopped.append(run_id)
            return None

        self.settings = Settings()
        self.settings.quotas.user_runs = self.settings.quotas.total_runs = 20
        self.settings.quotas.user_gpus = self.settings.quotas.total_gpus = 160
        self.settings.bench.poll_seconds = 0
        self.settings.bench.startup_grace_seconds = 0
        self.settings.datasets_root = str(tmp_path / "data")
        self.observer = ClusterObserver(self.runner, self.settings, mlsc_token=lambda: "tok")
        self.sessions = SessionManager(self.store, self.runner, self.paths, self.settings, token=lambda: "tok", mlsc_pod=pod, clock=lambda: self.now, platform="linux")

        async def fast_sleep(seconds: float) -> None:
            await asyncio.sleep(0)

        self.mgr = BenchManager(
            self.store, self.runner, self.paths, self.settings, secrets=lambda: {"MLSC_TOKEN": "tok", "AWS_ACCESS_KEY_ID_gcvmfleet": "a", "AWS_SECRET_ACCESS_KEY_gcvmfleet": "s"},
            mlsc_pod=pod, observer=self.observer, sessions=self.sessions, agent_limit=limit, stop_tasks=stop_tasks, clock=lambda: self.now, sleep=fast_sleep, platform="darwin",
        )

    def spec(self, **kw) -> BenchSpec:
        base = dict(run_id="run-x", worktree=self.worktree, run_dir=self.paths.runs / "run-x", overlay=self.overlay, recipe="glm", agent="terminus2",
                    dataset="birdsql", jobs=1, agents_per_replica=2, task_limit=3, model_name="GLM", pod="litellm-noauth-abc", preset="smoke", sha="a" * 40)
        base.update(kw)
        if "run_dir" not in kw and "run_id" in kw:
            base["run_dir"] = self.paths.runs / kw["run_id"]
        return BenchSpec(**base)

    async def start(self, **kw):
        run = await self.mgr.start(self.spec(**kw), ref="master", preview=Preview(model="GLM"))
        await self.mgr.tasks[run.id]
        return self.mgr.get(run.id)

    def session(self, status: str = "ready") -> Session:
        s = Session(id="console-glm-1", recipe="glm", status=status, created_at="t", model="GLM-s", replicas=3, version_id=41, dir=str(self.paths.runs / "s"))
        self.store.create_session(s)
        return s


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_plan_and_position() -> None:
    own = plan(True, 1)
    assert own == ["preflight", "materialize", "create_job", "start_llm", "check", "upload", "launch", "execute", "requeue", "launch", "execute", "stop_llm", "measure"]
    assert plan(False, 0) == ["preflight", "materialize", "upload", "launch", "execute", "measure"]
    assert position(own, "launch", 0) == 6 and position(own, "launch", 1) == 9 and position(own, "requeue", 0) == 8 and position(own, "measure", 1) == 12
    with pytest.raises(BenchError):
        position(own, "launch", 2)


async def test_bench_runs_all_stages_on_own_model(h: Harness) -> None:
    h.settings.bench.requeue_rounds = 1
    h.submitter = "done"
    run = await h.start()
    assert run.status == "done" and run.version_id == 1234 and run.stage == "" and run.finished_at
    assert h.stages == ["preflight", "preflight", "materialize", "create_job", "start_llm", "check", "upload", "launch", "requeue", "stop_llm", "measure"]
    stage_names = [s.stage for s in h.mgr.stages(run.id)]
    assert stage_names == ["preflight", "materialize", "create_job", "start_llm", "check", "upload", "launch", "execute", "requeue", "stop_llm", "measure"]
    assert all(s.returncode == 0 for s in h.mgr.stages(run.id))
    cmds = {s.stage: s.command for s in h.mgr.stages(run.id)}
    assert "main.run=[system.materialize,system.topology_saver,system.manifest_runner]" in cmds["materialize"]
    assert "# env: AWS_ACCESS_KEY_ID_gcvmfleet, AWS_SECRET_ACCESS_KEY_gcvmfleet, FLEET_DATASETS_ROOT, MLSC_TOKEN" in cmds["materialize"]
    assert "tok" not in cmds["create_job"] and "--print-config" in cmds["preflight"] and "цикл сервиса" in cmds["execute"]
    run_dir = Path(run.dir)
    assert (run_dir / "exp.yaml").exists() and (run_dir / "spec.json").exists() and (run_dir / "resolved.yaml").read_text().startswith("system:")
    assert (run_dir / "logs" / "materialize.log").exists() and (run_dir / "logs" / "materialize.exit").read_text().strip() == "0"
    own = h.store.get_run(run.id)
    assert own and own.version_id == 1234 and "запущен сервисом" in own.note
    envs = [meta["env"] for argv, meta in h.runner.calls if argv[:2] == ("uv", "run")]
    assert envs[0]["MLSC_TOKEN"] == "fleet-console-dummy" and envs[2]["MLSC_TOKEN"] == "tok" and envs[2]["FLEET_DATASETS_ROOT"].endswith("data")
    assert run.counters["submitter_alive"] is False and run.counters["completed"] == 7 and run.agent_limit is None  # версии 1234 в MLSC-фейке нет: лимит не трогали
    actions = [e.action for e in h.store.events()]
    assert actions[0] == "bench_done" and "bench_start" in actions and h.runner.calls[-1][0][0] == "osascript"


async def test_execute_loop_waits_for_queue_and_active_workflows(h: Harness) -> None:
    h.settings.bench.requeue_rounds = 0
    ticks = {"n": 0}

    async def scripted_sleep(seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 2:
            h.submitter = "done"  # очередь опустела, сабмиттер вышел
            h.argo_active = 1  # но одна задача ещё бежит
        if ticks["n"] == 3:
            h.argo_active = 0
        h.observer.runs.at = None  # заставить снимок обновиться
        await asyncio.sleep(0)

    h.mgr.sleep = scripted_sleep
    h.submitter = "alive"
    h.argo_active = 2
    run = await h.start()
    assert run.status == "done" and ticks["n"] == 3
    counters = run.counters
    assert counters["running"] == 0 and counters["queue"] == 0 and counters["submitted"] == 5 and counters["submitter_alive"] is False


async def test_bench_on_session_skips_model_stages_and_writes_limit_from_live_replicas(h: Harness) -> None:
    h.settings.bench.requeue_rounds = 0
    h.session()
    h.submitter = "done"
    link = SessionLink("console-glm-1", "GLM-s", "glm", 3, 41)
    h.runner.on("kubectl", "exec", "-n", NS, "fleet-submitter-python", "--", "curl", stdout='{"data":[{"id":"GLM-s"}]}')
    run = await h.start(session=link, model_name="")
    assert run.status == "done" and run.session_id == "console-glm-1" and run.model == "GLM-s" and run.jobs == 3 and run.version_id == 41
    assert h.stages == ["preflight", "preflight", "materialize", "upload", "launch", "measure"]  # без create_job/start_llm/check/stop_llm
    assert "mlsc.served_model_name=GLM-s" in h.mgr.stages(run.id)[1].command and "experiment.jobs=3" in h.mgr.stages(run.id)[1].command
    assert h.limits == [6] and run.agent_limit == 6  # версия 41: current 3 × 2 агента
    assert h.mgr.busy_sessions() == set() and not run.holds_gpu
    h.store.update_session("console-glm-1", status="stopping")
    with pytest.raises(BenchError, match="не готова"):
        await h.mgr.start(h.spec(run_id="run-y", session=link), ref="master", preview=Preview())


async def test_failures_before_and_after_gpu(h: Harness) -> None:
    h.fail = {"materialize"}
    run = await h.start()
    assert run.status == "failed" and "materialize не удалась" in run.error and "materialize упал" in run.error and run.version_id is None
    assert [e.action for e in h.store.events()][0] == "bench_fail"
    h.fail = {"check"}
    run = await h.start(run_id="run-y")
    assert run.status == "attention" and run.version_id == 1234 and run.holds_gpu and run.attention_at == run.created_at
    await asyncio.sleep(0)  # уведомление уходит отдельной задачей
    assert "check не удалась" in run.error and h.runner.calls[-1][0][0] == "osascript" and "GPU тратятся" in h.runner.calls[-1][0][-1]
    h.fail = {"preflight"}
    with pytest.raises(BenchError, match="Hydra: boom"):
        await h.start(run_id="run-z")
    assert h.store.get_bench("run-z") is None


async def test_retry_stage_and_stop(h: Harness) -> None:
    h.settings.bench.requeue_rounds = 0
    h.fail = {"upload"}
    h.submitter = "done"
    run = await h.start()
    assert run.status == "attention" and run.stage == "upload"
    h.fail = set()
    h.stages.clear()
    run = await h.mgr.retry(run.id)
    await h.mgr.tasks[run.id]
    run = h.mgr.get(run.id)
    assert run.status == "done" and h.stages == ["upload", "launch", "stop_llm", "measure"]
    with pytest.raises(BenchError):
        await h.mgr.retry(run.id)
    # остановка идущего прогона: лимит 0 и Terminate через stop_tasks, затем stop_llm своей модели
    h.submitter = "alive"
    hang = asyncio.Event()

    async def hanging_sleep(seconds: float) -> None:
        hang.set()
        await asyncio.sleep(3600)

    h.mgr.sleep = hanging_sleep
    h.stages.clear()
    started = await h.mgr.start(h.spec(run_id="run-y"), ref="master", preview=Preview())
    await asyncio.wait_for(hang.wait(), timeout=5)
    assert h.mgr.get("run-y").stage == "execute"
    stopped = await h.mgr.stop("run-y")
    assert stopped.status == "stopped" and h.stopped == ["run-y"] and h.stages[-1] == "stop_llm" and stopped.finished_at
    assert h.store.open_bench_stage("run-y") is None and [s.returncode for s in h.mgr.stages("run-y") if s.stage == "execute"] == [None]
    assert started.id not in h.mgr.tasks


async def test_execute_detects_dead_submitter_with_queue(h: Harness) -> None:
    h.settings.bench.requeue_rounds = 0
    h.submitter = "dead_with_queue"
    run = await h.start()
    assert run.status == "attention" and "в очереди 3 манифестов" in run.error and run.stage == "execute"
    h.submitter = "missing"
    run = await h.start(run_id="run-y")
    assert run.status == "attention" and "каталог прогона отсутствует" in run.error


async def test_requeue_round_relaunches_only_when_something_moved(h: Harness) -> None:
    h.settings.bench.requeue_rounds = 1
    h.submitter = "done"
    h.requeue_moves = True
    run = await h.start()
    assert run.status == "done" and run.requeues == 1
    assert h.stages == ["preflight", "preflight", "materialize", "create_job", "start_llm", "check", "upload", "launch", "requeue", "launch", "stop_llm", "measure"]
    assert [s.stage for s in h.mgr.stages(run.id)].count("execute") == 2


async def test_resume_after_restart(h: Harness) -> None:
    h.settings.bench.requeue_rounds = 0
    h.submitter = "done"
    done = await h.start()
    # 1) стадия upload шла и завершилась кодом 0, пока сервис лежал: exit-файл есть → прицепиться и идти дальше
    h.store.update_bench(done.id, status="running", stage="upload")
    sid = h.store.start_bench_stage(done.id, "upload", "cmd", str(Path(done.dir) / "logs" / "upload.log"), pid=4242)
    StageFiles.for_stage(Path(done.dir) / "logs", "upload").exit.write_text("0")
    # 2) create_job убит до записи version_id → attention
    no_version = await h.start(run_id="run-nv")
    h.store.update_bench(no_version.id, status="running", stage="create_job", version_id=None)
    h.store.start_bench_stage(no_version.id, "create_job", "cmd", "", pid=999999999)
    h.configuration["application_versions"].pop(str(no_version.version_id))
    (Path(no_version.dir) / "version_id").unlink()
    StageFiles.for_stage(Path(no_version.dir) / "logs", "create_job").clear()
    # 3) create_job убит, но version_id записан → продолжить со start_llm
    with_version = await h.start(run_id="run-wv")
    h.store.update_bench(with_version.id, status="running", stage="create_job", version_id=None)
    h.store.start_bench_stage(with_version.id, "create_job", "cmd", "", pid=999999999)
    StageFiles.for_stage(Path(with_version.dir) / "logs", "create_job").clear()
    # 4) перезапуск посреди execute → цикл продолжается
    in_execute = await h.start(run_id="run-ex")
    h.store.update_bench(in_execute.id, status="running", stage="execute")
    h.store.start_bench_stage(in_execute.id, "execute", "цикл", "")
    # 5) остановка прервана перезапуском → сверяем кластер
    stopping = await h.start(run_id="run-st")
    h.store.update_bench(stopping.id, status="stopping")
    for active in (done, in_execute):
        h.configuration["application_version_states"][str(active.version_id)] = {"planned": 1, "current": 1}
    h.stages.clear()
    await h.mgr.resume()
    await asyncio.gather(*h.mgr.tasks.values())
    assert h.mgr.get(done.id).status == "done" and h.store.bench_stages(done.id)[-1].stage == "measure"
    assert next(s for s in h.store.bench_stages(done.id) if s.id == sid).returncode == 0
    assert h.mgr.get(no_version.id).status == "attention" and "неоднозначен" in h.mgr.get(no_version.id).error
    assert h.mgr.get(with_version.id).status == "done", h.mgr.get(with_version.id).error
    assert h.mgr.get(with_version.id).version_id == with_version.version_id
    assert h.mgr.get(in_execute.id).status == "done" and h.mgr.get(stopping.id).status == "stopped"
    assert "create_job" not in h.stages and h.stages.count("start_llm") == 1
    # launch при живом сабмиттере не повторяется
    relaunch = await h.start(run_id="run-rl")
    h.stages.clear()
    h.store.update_bench(relaunch.id, status="running", stage="launch")
    h.store.start_bench_stage(relaunch.id, "launch", "cmd", "", pid=999999999)
    StageFiles.for_stage(Path(relaunch.dir) / "logs", "launch").clear()
    h.configuration["application_version_states"][str(relaunch.version_id)] = {"planned": 1, "current": 1}
    h.submitter = "alive"
    h.observer.runs.at = None

    async def finish_soon(seconds: float) -> None:
        h.submitter = "done"
        h.observer.runs.at = None
        await asyncio.sleep(0)

    h.mgr.sleep = finish_soon
    await h.mgr.resume()
    await asyncio.gather(*h.mgr.tasks.values())
    assert h.mgr.get(relaunch.id).status == "done" and "launch" not in h.stages
    assert any("пропущено: сабмиттер прогона уже жив" in s.command for s in h.store.bench_stages(relaunch.id))


async def test_reconcile_interrupted_stop_waits_for_all_remote_resources(h: Harness) -> None:
    h.fail = {"check"}
    run = await h.start()
    h.store.update_bench(run.id, status="stopping")
    h.argo_active = 1
    state = {"planned": 0, "current": 1}
    h.configuration["application_version_states"][str(run.version_id)] = state
    before = len(h.runner.calls)
    await h.mgr.resume()
    assert h.store.request(run.id)["state"] == "reserved"
    error = h.mgr.get(run.id).error
    assert "сабмиттер работает" in error and "активные workflows: 1" in error and "ещё активна" in error
    assert not h.stopped and all(argv[0] == "kubectl" for argv, _ in h.runner.calls[before:])
    h.submitter = "done"
    h.argo_active = 0
    await h.mgr.tick()
    assert h.mgr.get(run.id).status == "stopping"
    state["current"] = 0
    await h.mgr.tick()
    assert h.mgr.get(run.id).status == "stopped" and h.store.request(run.id)["state"] == "released"


async def test_resume_refuses_to_launch_on_stopped_model(h: Harness) -> None:
    h.submitter = "done"
    h.fail = {"upload"}
    run = await h.start()
    h.store.update_bench(run.id, status="running", stage="launch")
    await h.mgr.resume()
    await h.mgr.tasks[run.id]
    assert "погашена" in h.mgr.get(run.id).error
    assert "launch" not in h.stages


async def test_resume_lost_launch_acknowledgment_uses_argo_instead_of_relaunching(h: Harness) -> None:
    h.settings.bench.requeue_rounds = 0
    h.submitter = "done"
    h.fail = {"upload"}
    run = await h.start()
    h.store.update_bench(run.id, status="running", stage="launch")
    h.store.start_bench_stage(run.id, "launch", "interrupted")
    h.configuration["application_version_states"][str(run.version_id)] = {"planned": 1, "current": 1}
    h.argo_active = 1

    async def finish_workflows(_: float) -> None:
        h.argo_active = 0

    h.mgr.sleep = finish_workflows
    await h.mgr.resume()
    await asyncio.wait_for(h.mgr.tasks[run.id], 3)
    assert h.mgr.get(run.id).status == "done" and "launch" not in h.stages
    assert any("задачи прогона уже активны в Argo" in s.command for s in h.mgr.stages(run.id))


async def test_attention_auto_stop_and_measure_later(h: Harness) -> None:
    h.settings.bench.attention_minutes = 30
    h.fail = {"check"}
    run = await h.start()
    assert run.status == "attention" and run.holds_gpu
    h.now = T0 + timedelta(minutes=29)
    await h.mgr.tick()
    assert h.mgr.get(run.id).status == "attention"
    h.now = T0 + timedelta(minutes=31)
    h.fail = set()
    await h.mgr.tick()
    stopped = h.mgr.get(run.id)
    assert stopped.status == "stopped" and h.stages[-1] == "stop_llm" and any("автогашение" in e.details for e in h.store.events())
    measured = await h.mgr.measure(run.id)
    assert measured.status == "done" and h.stages[-1] == "measure"
    h.settings.bench.attention_minutes = 0
    h.fail = {"check"}
    other = await h.start(run_id="run-y")
    h.now = T0 + timedelta(hours=5)
    await h.mgr.tick()
    assert h.mgr.get(other.id).status == "attention"  # автогашение выключено


async def test_spec_roundtrip_and_dataset_json(h: Harness) -> None:
    from fleet_console.presets import DatasetRef
    from fleet_console.runs import dataset_of

    spec = h.spec(dataset=DatasetRef(path="cowork/tasks", benchmark="cowork", mapping=None), extra=("a=b",))
    spec.write()
    again = BenchSpec.from_json(spec.spec_file.read_text(), pod="pod-2")
    assert again.dataset == spec.dataset and again.extra == ("a=b",) and again.pod == "pod-2" and again.worktree == spec.worktree
    h.submitter = "done"
    h.settings.bench.requeue_rounds = 0
    run = await h.start(dataset=DatasetRef(path="cowork/tasks", benchmark="cowork"))
    assert json.loads(run.dataset)["benchmark"] == "cowork" and dataset_of(run).path == "cowork/tasks" and dataset_of(h.mgr.get("run-x")).benchmark == "cowork"


async def test_failed_attempt_recovery_reserves_quota_and_uses_existing_version(h: Harness, monkeypatch) -> None:
    from fleet_console.observe.s3 import S3Object

    h.submitter = "done"
    run = await h.start()
    h.store.update_bench(run.id, status="stopped")  # an older, already closed run
    rel = "b/a/results/t1"
    flags = [S3Object(rel + "/try3/FAILED", 0, ""), S3Object("b/a/results/t2/try2/COMPLETED", 0, "")]

    async def objects(_):
        return flags

    monkeypatch.setattr(h.mgr, "s3_objects", objects)
    with pytest.raises(BenchError, match="изменились"):
        await h.mgr.retry_failed(run.id, {rel: 2})
    h.argo_active = 1
    with pytest.raises(BenchError, match="активных workflows"):
        await h.mgr.retry_failed(run.id, {rel: 3})
    h.argo_active = 0
    h.settings.quotas.total_gpus = 0
    await h.mgr.retry_failed(run.id, {rel: 3})
    assert h.mgr.get(run.id).status == "queued"
    with pytest.raises(BenchError):
        await h.mgr.retry_failed(run.id, {rel: 3})

    orig = h.mgr._stage

    async def stage(rid, spec, name):
        if name == "start_llm":
            h.configuration["application_version_states"][str(run.version_id)] = {"planned": 1, "current": 1}
        if name == "launch":
            flags[0] = S3Object(rel + "/try4/COMPLETED", 0, "")
        return await orig(rid, spec, name)

    monkeypatch.setattr(h.mgr, "_stage", stage)
    h.runner.on("kubectl", "exec", "-i", "-n", NS)
    h.runner.on("kubectl", "exec", "-n", NS, "fleet-submitter-python", "--", "curl", stdout=json.dumps({"data": [{"id": run.model}]}))
    h.stages.clear()
    h.settings.quotas.total_gpus = 160
    await h.mgr.dispatch()
    await h.mgr.tasks[run.id]
    assert h.stages == ["start_llm", "check", "launch", "stop_llm", "measure"]
    assert h.mgr.get(run.id).failed_attempts == 0
    assert h.mgr.get(run.id).version_id == run.version_id
    assert h.store.request(run.id)["state"] == "released"
    cmd = next(meta for argv, meta in h.runner.calls if argv[:7] == ("kubectl", "exec", "-i", "-n", NS, "fleet-submitter-python", "--"))
    assert json.loads(cmd["stdin"])["failed"] == [run.preview["s3"] + "/" + rel]


def test_retry_manifest_selection_is_exact_and_resumable(tmp_path):
    from fleet_console.runs.retry_manifests import prepare

    root = tmp_path / "run"
    pending, submitted = root / "pending", root / "submitted"
    pending.mkdir(parents=True)
    submitted.mkdir()

    def manifest(folder, key):
        (folder / (key + ".yaml")).write_text(yaml.safe_dump({"metadata": {
            "labels": {"fleet/run-name": "run"}, "annotations": {"fleet/results-s3": "s3://b/" + key},
        }}))

    manifest(submitted, "failed")
    manifest(submitted, "completed")
    manifest(pending, "unrelated")
    with pytest.raises(ValueError):
        prepare(root, {"s3://b/failed"}, {"s3://b/completed"})
    assert (submitted / "failed.yaml").exists()
    (pending / "unrelated.yaml").unlink()
    for _ in range(2):
        prepare(root, {"s3://b/failed"}, {"s3://b/completed"})
    assert [p.name for p in pending.iterdir()] == ["failed.yaml"]
    assert [p.name for p in submitted.iterdir()] == ["completed.yaml"]


async def test_poll_timeout_recovers_but_zero_replicas_requires_attention(h, monkeypatch):
    from fleet_console.observe.mlsc import Version
    from fleet_console.observe.submitter import SubmitterRun
    from fleet_console.ui.benches import _status_text

    h.submitter = "done"
    run = await h.start()
    original = h.mgr._snapshot
    errors = 0

    async def flaky(run_id, spec, *, fresh):
        nonlocal errors
        errors += 1
        if errors < 3:
            raise BenchError("Не удалось получить свежий статус прогона: таймаут 60 с")
        return await original(run_id, spec, fresh=fresh)

    monkeypatch.setattr(h.mgr, "_snapshot", flaky)
    assert await h.mgr._execute(run.id, h.spec())
    assert errors == 3

    async def lost(run_id, spec, *, fresh):
        return {"argo": None, "sub": SubmitterRun(run_id, 4, 1, 2, 0, True, None),
                "version": Version(run.version_id, run_id, run.model, 1, 0)}

    monkeypatch.setattr(h.mgr, "_snapshot", lost)
    h.store.update_bench(run.id, status="running", stage="execute", requeues=0)
    await h.mgr._drive(run.id, h.spec(), h.mgr.plan_of(run).index("execute"), None, False)
    assert _status_text(h.mgr.get(run.id)) == "требует внимания · модель недоступна"
    assert h.store.open_bench_stage(run.id) is None
