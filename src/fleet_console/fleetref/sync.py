"""uv sync worktree и сборка команды запуска fleet из него."""

from __future__ import annotations

from pathlib import Path

from fleet_console.runner import LineHook, Result, Runner


def fleet_argv(worktree: Path, *args: str) -> list[str]:
    """`uv run --project <worktree> --no-sync fleet …` — без --no-sync uv синхронизирует окружение на каждый вызов."""
    return ["uv", "run", "--project", str(worktree), "--no-sync", "fleet", *args]


def venv_ready(worktree: Path) -> bool:
    return (worktree / ".venv" / "bin" / "fleet").exists()


def proxy_env(proxy: str | None) -> dict[str, str]:
    """pypi-mirror.gitverse.ru доступен только через SOCKS бастиона."""
    if not proxy:
        return {}
    return {"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "ALL_PROXY": proxy}


async def uv_sync(
    runner: Runner,
    worktree: Path,
    *,
    log_path: Path,
    proxy: str | None = None,
    on_line: LineHook | None = None,
) -> Result:
    """`uv sync --frozen` в worktree; вывод — в лог и в on_line."""
    return await runner.stream(
        ["uv", "sync", "--frozen"],
        cwd=worktree,
        env=proxy_env(proxy),
        log_path=log_path,
        on_line=on_line,
    )
