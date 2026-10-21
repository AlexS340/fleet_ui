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
