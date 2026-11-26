"""Контракт с Harbor без установленного Fleet и кластера."""

import json
import runpy
import sys
from fnmatch import fnmatch
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_benches import Harness

from fleet_console.compose import Preview
from fleet_console.runner import Result
from fleet_console.runs.manager import BenchError


class LocalId:
    def __init__(self, path):
        self.path = path

    def get_name(self):
        return self.path.name


class Dataset:
    """Harbor разрешает имена и лимит уже после отбрасывания невалидных путей."""

    def __init__(self, root, valid):
        self.path = root
        self.valid = valid
        self.task_names = []
        self.exclude = []
        self.limit = None

    def is_local(self):
        return True

    def _filter_task_ids(self, ids):
        return [i for i in ids if (not self.task_names or any(fnmatch(i.get_name(), p) for p in self.task_names))
                and i.get_name() not in self.exclude][:self.limit]

    async def get_task_configs(self, disable_verification):
        return self._filter_task_ids([LocalId(p) for p in self.path.iterdir() if self.valid(p, disable_verification)])


@pytest.fixture
def probe(tmp_path, monkeypatch):
    for name in ("task-a", "task-b"):
        (tmp_path / name).mkdir()
    first, second = tmp_path.iterdir()
    task = Mock(side_effect=ValueError("shared несовместим с [verifier.environment]"))
    task.is_valid_dir.side_effect = lambda path, disable_verification: disable_verification or path == second
    dataset = Dataset(tmp_path, task.is_valid_dir)
    job = SimpleNamespace(verifier=SimpleNamespace(disable=False), datasets=[dataset], tasks=[])
    source = Mock()
    source.model_validate.return_value.load.return_value = job
    monkeypatch.setitem(sys.modules, "fleet.harbor", SimpleNamespace(HarborJobConfigSource=source))
    monkeypatch.setitem(sys.modules, "harbor.models.task.id", SimpleNamespace(LocalTaskId=LocalId))
    monkeypatch.setitem(sys.modules, "harbor.models.task.task", SimpleNamespace(Task=task))
    script = Path(__file__).resolve().parents[1] / "src/fleet_console/compose/harbor_probe.py"
    check = runpy.run_path(str(script))["check_materialize"]

    async def run():
        return await check({"job_source": {"config": {}}})

    return run, job, dataset, first, second


async def test_path_loss_and_replacement_at_limit(probe):
    run, job, dataset, invalid, valid = probe
    dataset.task_names = ["task-*"]
    for limit in (None, 1):
        dataset.limit = limit
        with pytest.raises(ValueError) as failure:
            await run()
        assert str(invalid) in str(failure.value) and "[verifier.environment]" in str(failure.value)
    assert str(valid) in str(failure.value)  # тот же размер выборки, другая задача
    job.verifier.disable = True
    assert await run() == 1
    dataset.limit = None
    assert await run() == 2


async def test_intentional_subset_missing_and_empty(probe):
    run, _, dataset, invalid, valid = probe
    dataset.task_names = [valid.name]
    assert await run() == 1
    dataset.task_names.append("missing")
    with pytest.raises(ValueError, match="missing: нет каталога"):
        await run()
    dataset.task_names = []
    dataset.exclude = [invalid.name]
    assert await run() == 1
    dataset.exclude.append(valid.name)
    with pytest.raises(ValueError, match="пустая выборка"):
        await run()


async def test_failed_probe_blocks_submission_and_queued_materialize(tmp_path):
    harness = Harness(tmp_path)
    python = str(harness.worktree / ".venv/bin/python")
    attempts = iter([1, 0, 1])
    harness.runner.handlers.insert(0, ((python,), lambda argv: Result(argv, next(attempts), "1", "bad/task.toml")))
    with pytest.raises(BenchError, match="bad/task.toml"):
        await harness.mgr.start(harness.spec(), ref="master", preview=Preview(model="GLM"))
    assert not harness.store.benches()
    run = await harness.start()
    assert run.status == "failed" and "bad/task.toml" in run.error
    assert "materialize" not in harness.stages
    assert harness.mgr.stages(run.id)[0].returncode == 1
    calls = [meta for argv, meta in harness.runner.calls if argv[0] == python]
    assert len(calls) == 3 and all(meta["cwd"] == harness.worktree for meta in calls)
    assert all("MLSC_TOKEN" not in json.loads(meta["stdin"]) for meta in calls)


@pytest.mark.nicegui_main_file("tests/ui_main.py")
async def test_form_shows_failed_task_and_blocks_start(user):
    import fakes
    from fleet_console.connect.kube import KubeStatus

    ctx = fakes.CTX
    ctx.kube_status = KubeStatus(context="internal", server="s", allowed=True, ready=True, permissions={"get nodes": True})
    revision = await ctx.manager.prepare("master")
    python = str(revision.worktree / ".venv/bin/python")
    ctx.benches.runner.handlers.insert(0, ((python,), Result((python,), 1, "", "task-b: shared / [verifier.environment]")))
    await user.open("/runs")
    user.find("Запустить бенч").click()
    await user.should_see("Пресет")
    user.find("Превью и preflight").click()
    await user.should_see("Состав задач Harbor")
    await user.should_see("task-b: shared / [verifier.environment]")
    user.find(marker="accept-warnings").elements.pop().set_value(True)
    assert not user.find(marker="start-bench").elements.pop().enabled
