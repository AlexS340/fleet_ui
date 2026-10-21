"""Точка входа Fleet Console: страницы NiceGUI и запуск сервера."""

from __future__ import annotations

from nicegui import ui


def main() -> None:
    """Запустить UI на 127.0.0.1:8080."""

    @ui.page("/")
    def index() -> None:
        with ui.header().classes("items-center"):
            ui.label("Fleet Console")
        ui.label("Каркас Ф0: страницы появятся на следующих шагах.")

    ui.run(host="127.0.0.1", port=8080, title="Fleet Console", reload=False, show=False, dark=None)
