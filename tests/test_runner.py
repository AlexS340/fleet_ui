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
    assert fake.calls[0] == (("git", "fetch", "--prune"), {"cwd": tmp_path, "env": {}, "timeout": None})
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
