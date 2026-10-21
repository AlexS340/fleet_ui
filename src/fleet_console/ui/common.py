"""Общие кусочки UI: диалог подтверждения разрушительного действия, вопросик с подсказкой."""

from __future__ import annotations

from nicegui import ui


def hint(text: str) -> ui.icon:
    """Вопросик рядом с термином: объяснение по наведению, экран не перегружается."""
    icon = ui.icon("help_outline").classes("text-base opacity-50 cursor-help")
    with icon:
        ui.tooltip(text).props('max-width="24rem"').classes("text-sm")
    return icon


def confirm_dialog(title: str, lines: list[str], action, after=None, button: str = "Выполнить") -> None:
    """Сводка перед действием, которое меняет кластер или тратит GPU; ошибка остаётся в диалоге."""
    with ui.dialog() as dialog, ui.card().classes("min-w-80 max-w-xl"):
        ui.label(title).classes("text-lg break-all")
        for line in lines:
            ui.label(line).classes("text-sm")
        status = ui.label().classes("text-sm whitespace-pre-wrap")

        async def run() -> None:
            status.set_text("выполняю…")
            status.classes(remove="text-negative")
            try:
                await action()
            except Exception as exc:  # noqa: BLE001 — текст ошибки kubectl или fleet показываем как есть
                status.set_text(str(exc))
                status.classes("text-negative")
                return
            dialog.close()
            if after:
                after()
            ui.notify("Готово")

        with ui.row().classes("gap-2"):
            ui.button(button, on_click=run).props("unelevated no-caps color=negative")
            ui.button("Отмена", on_click=dialog.close).props("flat no-caps")
    dialog.open()
