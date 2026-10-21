from pathlib import Path

from fleet_console.guard import Battery, SleepGuard, parse_pmset
from fleet_console.runner import FakeRunner

PMSET_BATTERY = (
    "Now drawing from 'Battery Power'\n"
    " -InternalBattery-0 (id=4587619)\t63%; discharging; 3:12 remaining present: true\n"
)
PMSET_AC = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=4587619)\t100%; charged; 0:00 remaining present: true\n"


class FakeProc:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def make(tmp_path: Path, platform: str = "darwin", runner: FakeRunner | None = None):
    spawned: list[tuple[list[str], FakeProc]] = []

    async def spawner(argv, log_path):
        proc = FakeProc()
        spawned.append((list(argv), proc))
        return proc

    return SleepGuard(runner, spawner, platform=platform, pid=42, log_path=tmp_path / "caffeinate.log"), spawned


async def test_guard_spawns_once_releases_and_restarts_dead_process(tmp_path: Path) -> None:
    guard, spawned = make(tmp_path)
    assert "выключена" in guard.status() and not guard.active
    await guard.update({"run-a": "прогон run-a: задач идёт 3"})
    assert spawned[0][0] == ["caffeinate", "-i", "-s", "-w", "42"] and guard.active and "1 акт." in guard.status()
    await guard.update({"run-a": "…", "run-b": "…"})
    assert len(spawned) == 1 and "2 акт." in guard.status()  # второй процесс не нужен
    spawned[0][1].returncode = 1  # caffeinate умер сам
    await guard.update({"run-a": "…"})
    assert len(spawned) == 2 and guard.active
    await guard.update({})
    assert spawned[1][1].killed and not guard.active and not guard.reasons


async def test_guard_outside_macos_and_spawn_failure(tmp_path: Path) -> None:
    guard, spawned = make(tmp_path, platform="linux")
    await guard.update({"a": "сессия glm"})
    assert not spawned and not guard.active and "не нужна" in guard.status() and guard.reasons

    async def broken(argv, log_path):
        raise OSError("caffeinate not found")

    failing = SleepGuard(None, broken, platform="darwin", pid=1)
    await failing.update({"a": "a"})
    assert not failing.active and "не поднялась" in failing.status() and "not found" in failing.error


async def test_battery_parsing_and_refresh(tmp_path: Path) -> None:
    assert parse_pmset(PMSET_BATTERY) == Battery(False, 63) and parse_pmset(PMSET_AC) == Battery(True, 100)
    assert parse_pmset("garbage") == Battery(False, None)
    runner = FakeRunner()
    runner.on("pmset", stdout=PMSET_BATTERY)
    guard, _ = make(tmp_path, runner=runner)
    await guard.refresh_battery()
    assert guard.battery == Battery(False, 63)
    linux, _ = make(tmp_path, platform="linux", runner=runner)
    await linux.refresh_battery()
    assert linux.battery is None and not any(c[0][0] == "pmset" for c in FakeRunner().calls)
