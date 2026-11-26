"""История Argo и расследование ошибок на странице кластера."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from nicegui import ui

from fleet_console.context import AppContext
from fleet_console.observe.argo_history import DAY


def _time(stamp: int) -> str:
    return datetime.fromtimestamp(stamp, UTC).astimezone().strftime("%d.%m %H:%M:%S %Z")


def _col(name: str, label: str) -> dict:
    return {"name": name, "field": name, "label": label, "align": "left"}


def _live(ctx: AppContext, run: str) -> None:
    obs = ctx.observer
    for source in (obs.runs, obs.submitter):
        data = obs.current(source)
        if data is None or not source.at:
            ui.label(f"{source.title} · текущие данные недоступны").classes("text-sm")
            continue
        if source is obs.runs:
            match = next((r for r in data if r.run_name == run), None)
            text = f"Workflow в работе {match.running if match else 0}, в ожидании {match.pending if match else 0}"
        else:
            sub = next((s for s in data if s.run_id == run), None)
            text = "Сабмиттер не найден"
            if sub:
                state = "работает" if sub.alive else "остановлен"
                text = f"Сабмиттер {state}, в очереди {sub.pending}"
        ui.label(f"{text} · опрос {_time(int(source.at.timestamp()))}").classes("text-sm")


async def _workflow(ctx: AppContext, scope: str, cutoff: int, name: str) -> None:
    ctx.access.require_admin()
    row = ctx.argo_history.find(scope, cutoff, name)
    if row is None:
        ui.notify("За последние сутки этот Workflow не записан")
        return
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
        ui.label(row["name"]).classes("text-lg break-all")
        ui.label(f"Прогон {row['run']}").classes("break-all")
        ui.label(f"{row['phase']} · {_time(row['finished'])}")
        failed = row["phase"] in ("Failed", "Error")
        if failed:
            cause = ui.label().classes("whitespace-pre-wrap break-words max-w-full font-medium")
        if row["message"]:
            ui.label(f"Argo · {row['message']}").classes("break-words max-w-full")
        if row["node_error"]:
            ui.label(f"Под · {row['node_error']}").classes("break-words max-w-full")
        _live(ctx, row["run"])
        ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
    dialog.open()
    if failed:
        cause.set_text("Читаю причину из S3…")
        try:
            reason = row["pipeline"]
            if not reason and row["results_uri"]:
                reason = await ctx.argo_history.pipeline_error(row, ctx.benches.aws_env())
            ctx.access.require_admin()
            cause.set_text(
                f"Ошибка задачи · {reason}" if reason else "Причина из pipeline.log для этого Workflow недоступна"
            )
        except (RuntimeError, ValueError, OSError):
            cause.set_text("Не удалось прочитать причину из S3")


def _run(ctx: AppContext, scope: str, cutoff: int, name: str) -> None:
    ctx.access.require_admin()
    offset = 0
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-3xl"):
        ui.label(name).classes("text-lg break-all")
        _live(ctx, name)
        table = (
            ui.table(
                columns=[_col("name", "Workflow"), _col("phase", "Фаза"), _col("time", "Завершён")],
                rows=[],
                row_key="name",
                pagination=0,
            )
            .props("flat dense hide-bottom")
            .classes("w-full")
        )

        async def open_workflow(e):
            await _workflow(ctx, scope, cutoff, e.args[1]["name"])

        table.on("rowClick", open_workflow)

        def page(delta: int) -> None:
            nonlocal offset
            ctx.access.require_admin()
            offset = max(0, offset + delta)
            rows = ctx.argo_history.workflows(scope, cutoff, name, offset)
            table.update_rows(
                [{"name": r["name"], "phase": r["phase"], "time": _time(r["finished"])} for r in rows[:50]]
            )
            prev.set_visibility(offset > 0)
            more.set_visibility(len(rows) > 50)

        with ui.row():
            prev = ui.button("Назад", on_click=lambda: page(-50)).props("flat no-caps")
            more = ui.button("Следующие 50", on_click=lambda: page(50)).props("flat no-caps")
            ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
        page(0)
    dialog.open()


def history_panel(ctx: AppContext) -> None:
    ctx.access.require_admin()
    with ui.expansion("История Workflow").classes("w-full") as panel:
        with ui.row().classes("w-full items-end gap-2 flex-wrap"):
            period = ui.toggle({1: "За час", 24: "За сутки"}, value=1).props("no-caps")
            query = ui.input("Имя Workflow из алерта").classes("grow min-w-48").props("clearable")

            async def search() -> None:
                name = (query.value or "").strip().removeprefix("workflow/")
                if name:
                    await _workflow(ctx, ctx.history_scope, int(time.time()) - DAY, name)

            ui.button("Найти", on_click=search).props("no-caps")
            query.on("keydown.enter", search)
        status = ui.label().classes("text-xs opacity-70")
        table = (
            ui.table(
                columns=[
                    _col("run", "Прогон"),
                    _col("failed", "Упали"),
                    _col("succeeded", "Успешно"),
                    _col("last", "Последнее завершение"),
                ],
                rows=[],
                row_key="run",
                pagination=10,
            )
            .props("flat dense")
            .classes("w-full")
        )
        table.on("rowClick", lambda e: _run(ctx, ctx.history_scope, cutoff(), e.args[1]["run"]))

        def cutoff() -> int:
            return int(time.time()) - int(period.value) * 3600

        def refresh() -> None:
            if not panel.value:
                return
            ctx.access.require_admin()
            history = ctx.argo_history
            rows = history.groups(ctx.history_scope, cutoff())
            table.update_rows([dict(r) | {"last": _time(r["last"])} for r in rows])
            table.set_visibility(bool(rows))
            table.props("hide-bottom" if len(rows) <= 10 else None, remove="hide-bottom" if len(rows) > 10 else None)
            checkpoint = history.checkpoint(ctx.history_scope)
            note = f"Наблюдение с {_time(checkpoint['since'])}" if checkpoint else "Ожидание первого снимка"
            if history.error:
                note += f" · ошибка наблюдения: {history.error}"
            elif not ctx.kube_status.ready:
                note += " · нет связи с кластером"
            if not rows:
                note += " · за выбранный период завершений не записано"
            status.set_text(note)

        panel.on_value_change(refresh)
        period.on_value_change(refresh)
        ui.timer(10, refresh)
