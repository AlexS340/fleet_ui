from pathlib import Path
import sys

from fleet_console.runner import CommandError, FakeRunner, Result, SubprocessRunner


async def test_subprocess_run_and_stream(tmp_path: Path) -> None:
    runner = SubprocessRunner()
    result = await runner.run([sys.executable, "-c", "import os; print(os.environ['PROBE'])"], env={"PROBE": "да"})
    assert result.ok and result.stdout.strip() == "да"

    log = tmp_path / "x.log"
    seen: list[str] = []
    result = await runner.stream(
        [sys.executable, "-c", "import sys; print('a'); print('b', file=sys.stderr); sys.exit(3)"],
        log_path=log,
        on_line=seen.append,
    )
    assert result.returncode == 3
    assert sorted(seen) == ["a", "b"]
    text = log.read_text(encoding="utf-8")
    assert text.startswith("$ ") and text.rstrip().endswith("→ код 3")


async def test_subprocess_timeout() -> None:
    result = await SubprocessRunner().run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
    assert result.returncode == -1 and "таймаут" in result.stderr


async def test_fake_runner_prefix_and_calls(tmp_path: Path) -> None:
    fake = FakeRunner()
    fake.on("git", "fetch", stdout="ok")
    fake.on("uv", result=lambda argv: Result(argv, 1, "", "boom"))
    assert (await fake.run(["git", "fetch", "--prune"], cwd=tmp_path)).stdout == "ok"
    assert (await fake.run(["uv", "sync"])).returncode == 1
    assert (await fake.run(["kubectl"])).returncode == 127
    assert fake.calls[0] == (("git", "fetch", "--prune"), {"cwd": tmp_path, "env": {}, "timeout": None, "stdin": None})
    log = tmp_path / "s.log"
    lines: list[str] = []
    await fake.stream(["git", "fetch"], log_path=log, on_line=lines.append)
    assert lines == ["ok"] and log.read_text() == "ok"


def test_command_error_message() -> None:
    err = CommandError(Result(("git", "x"), 2, "", "line1\nline2"))
    assert "git x → код 2" in str(err) and "line2" in str(err)


async def test_stream_splits_carriage_returns(tmp_path: Path) -> None:
    seen: list[str] = []
    code = "import sys; sys.stdout.write('Counting 1%\\rCounting 2%\\rCounting 100%\\ndone\\n')"
    result = await SubprocessRunner().stream([sys.executable, "-c", code], log_path=tmp_path / "p.log", on_line=seen.append)
    assert result.ok and seen == ["Counting 1%", "Counting 2%", "Counting 100%", "done"]


async def test_child_env_drops_virtual_env_and_colors(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/some/venv")
    monkeypatch.delenv("NO_COLOR", raising=False)
    code = "import os; print(os.environ.get('VIRTUAL_ENV', 'unset'), os.environ.get('NO_COLOR'))"
    result = await SubprocessRunner().run([sys.executable, "-c", code])
    assert result.stdout.split() == ["unset", "1"]
    seen: list[str] = []
    code = "print('\\x1b[1m\\x1b[33mwarning\\x1b[39m: plain')"
    await SubprocessRunner().stream([sys.executable, "-c", code], on_line=seen.append)
    assert seen == ["warning: plain"]


async def test_detached_stage_survives_and_reports_exit_code(tmp_path: Path) -> None:
    from fleet_console.runner import StageFiles, attach, kill_stage

    files = StageFiles.for_stage(tmp_path / "logs", "materialize")
    code = "import sys; print('материализовано 3'); print('warn', file=sys.stderr); sys.exit(4)"
    proc = await SubprocessRunner().detach([sys.executable, "-c", code], files=files, env={"PROBE": "x"})
    assert files.read_pid() == proc.pid
    assert await proc.wait() == 4
    text = files.log.read_text(encoding="utf-8")
    assert text.startswith("$ ") and "материализовано 3" in text and "warn" in text
    assert files.read_exit() == 4 and await attach(files) == 4  # повторный attach читает код из файла
    assert not files.alive() and not kill_stage(files)

    files.clear()
    assert files.read_pid() is None and files.read_exit() is None and files.log.exists()
    files.pid.write_text("999999999")  # мёртвый pid без кода выхода — стадию убили
    assert await attach(files, poll=0.01) is None


async def test_detached_stage_can_be_attached_and_killed(tmp_path: Path) -> None:
    import asyncio

    from fleet_console.runner import StageFiles, attach, kill_stage

    files = StageFiles.for_stage(tmp_path / "logs", "execute")
    proc = await SubprocessRunner().detach([sys.executable, "-c", "import time; time.sleep(30)"], files=files)
    await asyncio.sleep(0.2)
    assert files.alive()
    waiter = asyncio.create_task(attach(files, poll=0.05))
    assert kill_stage(files)
    code = await asyncio.wait_for(waiter, timeout=5)
    assert code is None and not files.alive()  # убита вся группа вместе с sh: кода выхода нет — так менеджер узнаёт «убили»
    await proc.wait()


async def test_fake_runner_detach_writes_stage_files(tmp_path: Path) -> None:
    from fleet_console.runner import StageFiles

    fake = FakeRunner()
    fake.on("uv", "run", stdout="ok\n", returncode=0)
    files = StageFiles.for_stage(tmp_path, "s")
    proc = await fake.detach(["uv", "run", "fleet"], files=files, cwd=tmp_path)
    assert await proc.wait() == 0 and files.read_exit() == 0 and "ok" in files.log.read_text()
    assert fake.calls[-1][1]["files"] is files
