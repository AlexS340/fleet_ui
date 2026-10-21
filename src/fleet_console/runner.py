"""Запуск внешних команд (git, uv, kubectl, ssh) через подменяемый Runner."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import os
from pathlib import Path
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


def _merged_env(extra: Mapping[str, str] | None) -> dict[str, str]:
    env = dict(os.environ)
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
    ) -> Result:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=_merged_env(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
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
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip("\n")
                lines.append(line)
                if log:
                    log.write(line + "\n")
                    log.flush()
                if on_line:
                    on_line(line)
            code = await proc.wait()
            if log:
                log.write(f"→ код {code}\n")
        finally:
            if log:
                log.close()
        return Result(tuple(argv), code, "\n".join(lines), "")


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

    async def run(self, argv: Argv, *, cwd: Path | None = None, env=None, timeout=None) -> Result:
        return await self._answer(argv, {"cwd": cwd, "env": dict(env or {}), "timeout": timeout})

    async def stream(self, argv: Argv, *, cwd=None, env=None, log_path=None, on_line=None) -> Result:
        result = await self._answer(argv, {"cwd": cwd, "env": dict(env or {}), "log_path": log_path})
        if log_path:
            Path(log_path).write_text(result.stdout, encoding="utf-8")
        if on_line:
            for line in result.stdout.splitlines():
                on_line(line)
        return result
