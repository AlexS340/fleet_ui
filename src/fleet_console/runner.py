"""Запуск внешних команд (git, uv, kubectl, ssh) через подменяемый Runner."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shlex
import signal
from typing import Protocol

Argv = Sequence[str]
LineHook = Callable[[str], None]


@dataclass(frozen=True)
class Result:
    """Результат завершённой команды."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def command(self) -> str:
        return shlex.join(self.argv)


class CommandError(RuntimeError):
    """Команда завершилась с ненулевым кодом."""

    def __init__(self, result: Result) -> None:
        tail = (result.stderr or result.stdout).strip().splitlines()[-5:]
        super().__init__(f"{result.command} → код {result.returncode}\n" + "\n".join(tail))
        self.result = result


class Runner(Protocol):
    """Интерфейс запуска команд: тесты подставляют FakeRunner."""

    async def run(
        self,
        argv: Argv,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        stdin: str | None = None,
    ) -> Result: ...

    async def stream(
        self,
        argv: Argv,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        log_path: Path | None = None,
        on_line: LineHook | None = None,
    ) -> Result: ...

    async def detach(
        self,
        argv: Argv,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        files: "StageFiles",
    ) -> "Detached": ...


@dataclass(frozen=True)
class StageFiles:
    """Файлы отделённой стадии: лог пишет сам дочерний процесс, pid и код выхода нужны для возобновления."""

    log: Path
    pid: Path
    exit: Path

    @classmethod
    def for_stage(cls, logs_dir: Path, name: str) -> "StageFiles":
        return cls(logs_dir / f"{name}.log", logs_dir / f"{name}.pid", logs_dir / f"{name}.exit")

    def read_pid(self) -> int | None:
        return _read_int(self.pid)

    def read_exit(self) -> int | None:
        return _read_int(self.exit)

    def clear(self) -> None:
        """Перед новым запуском: старые pid и код выхода не должны выдать себя за свежие. Лог остаётся (дописывается)."""
        for path in (self.pid, self.exit):
            path.unlink(missing_ok=True)

    def alive(self) -> bool:
        pid = self.read_pid()
        return pid is not None and self.read_exit() is None and pid_alive(pid)


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def detached_argv(argv: Argv, files: StageFiles) -> list[str]:
    """Обёртка sh: вывод в лог пишет сам процесс, код выхода — в файл; так стадия переживает перезапуск сервиса."""
    script = (
        f"{shlex.join(argv)} >> {shlex.quote(str(files.log))} 2>&1; code=$?; "
        f"echo $code > {shlex.quote(str(files.exit))}; exit $code"
    )
    return ["sh", "-c", script]


class Detached(Protocol):
    pid: int

    async def wait(self) -> int: ...


class DetachedProcess:
    """Стадия, запущенная этим процессом сервиса: ждём штатно, код берём из exit-файла."""

    def __init__(self, proc: asyncio.subprocess.Process, files: StageFiles) -> None:
        self.pid = proc.pid
        self.proc = proc
        self.files = files

    async def wait(self) -> int:
        await self.proc.wait()
        code = self.files.read_exit()
        return code if code is not None else (self.proc.returncode or 0)


async def attach(files: StageFiles, poll: float = 1.0) -> int | None:
    """После перезапуска сервиса: дождаться кода выхода живой стадии. None — процесс мёртв, а кода нет (убит)."""
    while True:
        code = files.read_exit()
        if code is not None:
            return code
        pid = files.read_pid()
        if pid is None or not pid_alive(pid):
            return files.read_exit()
        await asyncio.sleep(poll)


def kill_stage(files: StageFiles) -> bool:
    """SIGTERM всей группе процессов стадии (setsid): uv, fleet и их дети."""
    pid = files.read_pid()
    if pid is None or not pid_alive(pid):
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    return True


ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _merged_env(extra: Mapping[str, str] | None) -> dict[str, str]:
    """Окружение дочерней команды: без VIRTUAL_ENV (сервис сам живёт в venv uv, дочернему uv это мешает) и без цветов."""
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env.setdefault("NO_COLOR", "1")
    if extra:
        env.update(extra)
    return env


class SubprocessRunner:
    """Настоящий запуск через asyncio.create_subprocess_exec."""

    async def run(
        self,
        argv: Argv,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        stdin: str | None = None,
    ) -> Result:
        """stdin — для секретов (токен в `kubectl exec -i`): в argv и логи они не попадают."""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=_merged_env(env),
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode() if stdin is not None else None), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return Result(tuple(argv), -1, "", f"таймаут {timeout} с")
        return Result(tuple(argv), proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))

    async def stream(
        self,
        argv: Argv,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        log_path: Path | None = None,
        on_line: LineHook | None = None,
    ) -> Result:
        """Запустить команду, построчно писать объединённый вывод в лог и в on_line."""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=_merged_env(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        lines: list[str] = []
        log = log_path.open("a", encoding="utf-8") if log_path else None
        try:
            if log:
                log.write(f"$ {shlex.join(argv)}\n")
            assert proc.stdout is not None
            # git и uv рисуют прогресс через \r: считаем его разделителем строк, иначе одна строка на весь clone.
            buffer = b""
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    cut = min((i for i in (buffer.find(b"\n"), buffer.find(b"\r")) if i >= 0), default=-1)
                    if cut < 0:
                        break
                    raw, buffer = buffer[:cut], buffer[cut + 1 :]
                    _emit(raw.decode(errors="replace"), lines, log, on_line)
            if buffer:
                _emit(buffer.decode(errors="replace"), lines, log, on_line)
            code = await proc.wait()
            if log:
                log.write(f"→ код {code}\n")
        finally:
            if log:
                log.close()
        return Result(tuple(argv), code, "\n".join(lines), "")

    async def detach(
        self,
        argv: Argv,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        files: StageFiles,
    ) -> DetachedProcess:
        """Запустить стадию в собственной сессии (setsid): перезапуск сервиса её не убьёт, лог и код выхода на диске."""
        files.log.parent.mkdir(parents=True, exist_ok=True)
        files.clear()
        with files.log.open("a", encoding="utf-8") as log:
            log.write(f"$ {shlex.join(argv)}\n")
        proc = await asyncio.create_subprocess_exec(
            *detached_argv(argv, files),
            cwd=cwd,
            env=_merged_env(env),
            start_new_session=True,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        files.pid.write_text(str(proc.pid), encoding="utf-8")
        return DetachedProcess(proc, files)


def _emit(line: str, lines: list[str], log, on_line: LineHook | None) -> None:
    line = ANSI.sub("", line).rstrip()
    if not line:
        return
    lines.append(line)
    if log:
        log.write(line + "\n")
        log.flush()
    if on_line:
        on_line(line)


Handler = Callable[[tuple[str, ...]], Result | Awaitable[Result]]


@dataclass
class FakeRunner:
    """Подмена для тестов: ответы по префиксу argv, журнал вызовов."""

    handlers: list[tuple[tuple[str, ...], Result | Handler]] = field(default_factory=list)
    calls: list[tuple[tuple[str, ...], dict]] = field(default_factory=list)

    def on(self, *prefix: str, result: Result | Handler | None = None, stdout: str = "", returncode: int = 0) -> None:
        self.handlers.append((prefix, result or Result(prefix, returncode, stdout)))

    async def _answer(self, argv: Argv, meta: dict) -> Result:
        argv_t = tuple(argv)
        self.calls.append((argv_t, meta))
        for prefix, result in self.handlers:
            if argv_t[: len(prefix)] == prefix:
                if callable(result):
                    out = result(argv_t)
                    return await out if asyncio.iscoroutine(out) else out
                return Result(argv_t, result.returncode, result.stdout, result.stderr)
        return Result(argv_t, 127, "", f"FakeRunner: нет ответа для {shlex.join(argv_t)}")

    async def run(self, argv: Argv, *, cwd: Path | None = None, env=None, timeout=None, stdin=None) -> Result:
        return await self._answer(argv, {"cwd": cwd, "env": dict(env or {}), "timeout": timeout, "stdin": stdin})

    async def stream(self, argv: Argv, *, cwd=None, env=None, log_path=None, on_line=None) -> Result:
        result = await self._answer(argv, {"cwd": cwd, "env": dict(env or {}), "log_path": log_path})
        if log_path:
            Path(log_path).write_text(result.stdout, encoding="utf-8")
        if on_line:
            for line in result.stdout.splitlines():
                on_line(line)
        return result

    async def detach(self, argv: Argv, *, cwd=None, env=None, files: StageFiles) -> "FakeDetached":
        """Как настоящая стадия, только мгновенно: лог, pid и код выхода на диске."""
        result = await self._answer(argv, {"cwd": cwd, "env": dict(env or {}), "files": files})
        files.log.parent.mkdir(parents=True, exist_ok=True)
        files.clear()
        with files.log.open("a", encoding="utf-8") as log:
            log.write(f"$ {shlex.join(argv)}\n{result.stdout}\n")
        files.pid.write_text("4242", encoding="utf-8")
        files.exit.write_text(str(result.returncode), encoding="utf-8")
        return FakeDetached(4242, result.returncode)


@dataclass
class FakeDetached:
    pid: int
    code: int

    async def wait(self) -> int:
        return self.code
