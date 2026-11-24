"""Общие диалоги и подсказки UI."""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable

from nicegui import ui

from fleet_console.models.compose import JOB_PRIORITIES_BY_QUEUE
from fleet_console.settings import scrub

JOB_PRIORITY_OVERRIDE = "++mlsc.create_job.job.job_priority_class"
JOB_PRIORITIES = tuple(p for priorities in JOB_PRIORITIES_BY_QUEUE.values() for p in priorities)
JOB_QUEUE_KEY = "mlsc.create_job.job.queue_name"
JOB_QUEUES = tuple(JOB_PRIORITIES_BY_QUEUE)
DEFAULT_JOB_PRIORITIES = {"gigacode": "medium", "shared": "shared-low"}


def checkpoint_select(paths: list[str]) -> ui.select:
    names = {path: path.rstrip("/").rsplit("/", 1)[-1] for path in paths if path}
    counts = Counter(names.values())
    options = {"": "Из рецепта", **{path: name if counts[name] == 1 else path for path, name in names.items()}}
    return ui.select(options, value="", label="Чекпоинт", with_input=True).props('popup-content-style="overflow-wrap: anywhere"').classes("w-full min-w-0")


def restore_checkpoint(field: ui.select, overrides: list[str]) -> list[str]:
    """Переносим одиночный знакомый путь в поле, сохраняя сложные ручные overrides дословно."""
    matches = [entry for entry in overrides if entry.partition("=")[0].lstrip("+~") == "mlsc.model_path"]
    if len(matches) == 1 and matches[0].startswith("mlsc.model_path="):
        path = matches[0].partition("=")[2]
        if path and path in field.options:
            field.set_value(path)
            return [entry for entry in overrides if entry != matches[0]]
    field.set_value("")
    return overrides


def checkpoint_overrides(path: str, extra: str) -> list[str]:
    entries = [line.strip() for line in extra.splitlines() if line.strip()]
    if path:
        if any(entry.partition("=")[0].lstrip("+~") == "mlsc.model_path" for entry in entries):
            raise ValueError("Чекпоинт уже выбран. Уберите mlsc.model_path из overrides или выберите «Из рецепта».")
        entries.append(f"mlsc.model_path={path}")
    return entries


def set_job_priority_options(priority: ui.select, queue: str, preferred: str) -> None:
    options = {p: p for p in JOB_PRIORITIES_BY_QUEUE.get(queue, JOB_PRIORITIES)}
    if queue not in JOB_PRIORITIES_BY_QUEUE:
        options = {"": "Из рецепта", **options}
    priority.set_options(options, value=preferred if preferred in options else DEFAULT_JOB_PRIORITIES.get(queue, ""))


def job_options_select(queue_value: str = "", priority_value: str = "") -> tuple[ui.select, ui.select]:
    queue = ui.select({"": "Из рецепта", **{q: q for q in JOB_QUEUES}}, value=queue_value, label="Очередь MLSC").classes("w-36")
    priority = ui.select({}, label="Приоритет джобы").classes("w-40")
    set_job_priority_options(priority, queue_value, priority_value)
    queue.on_value_change(lambda e: set_job_priority_options(priority, e.value, priority.value if e.value else ""))
    return queue, priority


def split_job_options(overrides: list[str]) -> tuple[str, str, list[str]]:
    """Последние явные приоритет и очередь возвращаем в поля формы, остальные overrides — в текст."""
    remaining = list(overrides)
    selected = []
    for key, allowed in ((JOB_PRIORITY_OVERRIDE.lstrip("+"), JOB_PRIORITIES), (JOB_QUEUE_KEY, JOB_QUEUES)):
        matches = [entry for entry in remaining if entry.partition("=")[0].lstrip("+") == key]
        value = matches[-1].partition("=")[2] if matches else ""
        selected.append(value if value in allowed else "")
        if value in allowed:
            remaining = [entry for entry in remaining if entry not in matches]
    return selected[0], selected[1], remaining


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
