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
    """Маршрут uv: явно задаём или отключаем прокси, включая унаследованные переменные."""
    return {name: proxy or "" for name in (
        "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy",
    )}


async def uv_sync(
    runner: Runner,
    worktree: Path,
    *,
    log_path: Path,
    proxy: str | None = None,
    on_line: LineHook | None = None,
) -> Result:
    """Сначала напрямую; при сетевой ошибке — одна попытка через SOCKS бастиона."""
    for route in (None, proxy):
        message = "uv sync: напрямую" if route is None else "Прямое подключение не удалось; повтор uv sync через SOCKS"
        with log_path.open("a", encoding="utf-8") as log:
            log.write(message + "\n")
        if on_line:
            on_line(message)
        result = await runner.stream(
            ["uv", "sync", "--frozen"],
            cwd=worktree,
            env=proxy_env(route),
            log_path=log_path,
            on_line=on_line,
        )
        network_error = any(marker in (result.stdout + result.stderr).lower() for marker in (
            "error sending request", "operation timed out", "connection refused", "network is unreachable", "dns error",
        ))
        if result.ok or route is not None or not proxy or not network_error:
            return result
    return result
