"""Общие диалоги и подсказки UI."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from nicegui import ui

from fleet_console.settings import scrub

JOB_PRIORITY_OVERRIDE = "++mlsc.create_job.job.job_priority_class"
JOB_PRIORITIES = ("low", "medium", "high")
JOB_QUEUE_KEY = "mlsc.create_job.job.queue_name"
JOB_QUEUES = ("gigacode", "shared")


def job_priority_select() -> ui.select:
    return ui.select({"": "Из рецепта", **{priority: priority for priority in JOB_PRIORITIES}}, value="", label="Приоритет джобы").classes("w-40")


def job_queue_select() -> ui.select:
    return ui.select({"": "Из рецепта", **{queue: queue for queue in JOB_QUEUES}}, value="", label="Очередь MLSC").classes("w-36")


def hint(text: str) -> ui.icon:
    """Вопросик рядом с термином: объяснение по наведению, экран не перегружается."""
    icon = ui.icon("help_outline").classes("text-base opacity-50 cursor-help")
    with icon:
        ui.tooltip(text).props('max-width="24rem"').classes("text-sm")
    return icon


def text_dialog(title: str, text_coro: Awaitable[str], secrets: Callable[[], dict[str, str]]) -> None:
    """Диалог с текстом, который ещё читается. Секреты вырезаются при показе."""
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label(title).classes("text-xs opacity-60 break-all")
        body = ui.label("читаю…").classes("font-mono text-xs whitespace-pre-wrap break-all w-full").style("max-height: 65vh; overflow: auto")
        ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
    dialog.open()

    async def load() -> None:
        try:
            text = await text_coro
        except Exception as exc:  # noqa: BLE001 — ошибку команды показываем в диалоге
            body.set_text(scrub(str(exc), secrets().values()))
            body.classes("text-negative")
            return
        body.set_text(scrub(text, secrets().values()) or "(пусто)")

    ui.timer(0, load, once=True)


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
