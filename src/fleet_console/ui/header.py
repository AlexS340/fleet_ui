"""Шапка: context и namespace, статус пробросов, время fetch — на каждой странице."""

from __future__ import annotations

from datetime import datetime, timezone
import shlex

from nicegui import ui

from fleet_console.context import AppContext
from fleet_console.fleetref.manager import PrepareError

STATUS_COLORS = {"green": "positive", "yellow": "warning", "red": "negative", "off": "grey-7"}
STATUS_WORDS = {"green": "работает", "yellow": "переподключение", "red": "ошибка", "off": "выключен"}


def build_header(ctx: AppContext) -> None:
    """Шапка страницы. Refreshable создаётся на каждую страницу: общий на модуль копил бы контейнеры чужих клиентов."""

    @ui.refreshable
    def status_row() -> None:
        ks = ctx.kube_status
        ok = ks.allowed and ks.ready and not ks.missing_permissions
        kube_color = "positive" if ok else ("negative" if ks.error else "grey-7")
        _chip(f"{ks.context or 'context?'} · {ctx.settings.namespace}", kube_color, lambda: _kube_dialog(ctx))
        for fwd in ctx.supervisor.forwards:
            st = ctx.supervisor.state(fwd.name)
            _chip(fwd.title, STATUS_COLORS[st.status], lambda name=fwd.name: _forward_dialog(ctx, name))
        ui.label(_fetch_text(ctx)).classes("text-sm text-white opacity-80 hidden sm:inline")  # на узком экране шапка в 2 строки
        ui.button("Актуализировать", on_click=fetch).props("flat dense no-caps").classes("text-white")

    async def fetch() -> None:
        await _fetch(ctx)
        status_row.refresh()

    with ui.header().classes("items-center gap-2 flex-wrap px-4 py-2"):
        ui.link("Fleet Console", "/").classes("text-lg text-white no-underline mr-2")
        status_row()
        ui.space()
        ui.link("Настройки", "/settings").classes("text-white")
    ui.timer(2.0, status_row.refresh)


def _chip(text: str, color: str, on_click) -> None:
    ui.chip(text, color=color, text_color="white", on_click=on_click).props("clickable dense").classes("m-0")


def _fetch_text(ctx: AppContext) -> str:
    if not ctx.manager.mirror.exists():
        return "зеркала fleet ещё нет"
    at = ctx.manager.mirror.last_fetch_at()
    if at is None:
        return "fetch: никогда"
    minutes = int((datetime.now(timezone.utc) - at).total_seconds() // 60)
    return f"fetch: {minutes} мин назад" if minutes < 120 else f"fetch: {minutes // 60} ч назад"


async def _fetch(ctx: AppContext) -> None:
    if ctx.manager.progress.busy:
        ui.notify("Подождите: идёт другая операция с зеркалом")
        return
    try:
        await ctx.manager.fetch()
        ui.notify("Зеркало fleet обновлено")
    except PrepareError as exc:
        ui.notify(str(exc), type="negative", multi_line=True)


def _kube_dialog(ctx: AppContext) -> None:
    ks = ctx.kube_status
    with ui.dialog() as dialog, ui.card().classes("min-w-80"):
        ui.label("Кластер").classes("text-lg")
        ui.label(f"context: {ks.context or '—'}")
        ui.label(f"сервер: {ks.server or '—'}")
        ui.label(f"namespace: {ctx.settings.namespace}")
        ui.label(f"разрешённые context: {', '.join(ctx.settings.allowed_contexts)}")
        ui.label("API-сервер отвечает" if ks.ready else "API-сервер не отвечает").classes(
            "text-positive" if ks.ready else "text-negative"
        )
        if ks.error:
            ui.label(ks.error).classes("text-negative whitespace-pre-wrap")
        if ks.permissions:
            ui.label("Права:").classes("mt-2")
            for key, ok in ks.permissions.items():
                ui.label(("✓ " if ok else "✗ ") + key).classes("text-sm font-mono " + ("" if ok else "text-negative"))

        async def recheck() -> None:
            await ctx.refresh_kube(with_permissions=True)
            dialog.close()
            _kube_dialog(ctx)

        ui.button("Перепроверить", on_click=recheck).props("flat no-caps")
    dialog.open()


def _forward_dialog(ctx: AppContext, name: str) -> None:
    fwd = ctx.supervisor.forward(name)
    st = ctx.supervisor.state(name)
    with ui.dialog() as dialog, ui.card().classes("min-w-80 max-w-2xl"):
        ui.label(f"Проброс: {fwd.title}").classes("text-lg")
        ui.label(f"{STATUS_WORDS[st.status]}{' — ' + st.note if st.note else ''}").classes("text-" + STATUS_COLORS[st.status])
        ui.label(shlex.join(fwd.argv)).classes("font-mono text-sm whitespace-pre-wrap")
        if st.last_error:
            ui.label(st.last_error).classes("text-negative text-sm whitespace-pre-wrap")
        if fwd.hint:
            ui.label(fwd.hint).classes("text-sm opacity-80 whitespace-pre-wrap")
        log = ctx.supervisor.log_path(fwd)
        if log:
            ui.label(f"лог: {log}").classes("text-xs opacity-70")

        async def restart() -> None:
            await ctx.supervisor.restart(name)
            dialog.close()
            ui.notify(f"{fwd.title}: перезапуск")

        ui.button("Перезапустить", on_click=restart).props("flat no-caps")
    dialog.open()
