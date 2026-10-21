"""Запуск внешних команд (git, uv, kubectl, ssh) через подменяемый Runner."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shlex
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
