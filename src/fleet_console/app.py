"""Точка входа Fleet Console: страницы NiceGUI и запуск сервера."""

from __future__ import annotations

from nicegui import app, ui

from fleet_console.context import AppContext, build_context
from fleet_console.ui.revision import revision_page
from fleet_console.ui.settings import settings_page


def create_app(ctx: AppContext) -> None:
    """Зарегистрировать страницы и хуки жизненного цикла для данного контекста."""

    @ui.page("/")
    async def index() -> None:
        await revision_page(ctx)

    @ui.page("/settings")
    def settings() -> None:
        settings_page(ctx)

    app.on_startup(ctx.startup)
    app.on_shutdown(ctx.shutdown)


def main() -> None:
    """Запустить UI на 127.0.0.1:<ui_port>; тема следует за системной."""
    ctx = build_context()
    create_app(ctx)
    ui.run(host="127.0.0.1", port=ctx.settings.ui_port, title="Fleet Console", reload=False, show=False, dark=None)


if __name__ == "__main__":
    main()
