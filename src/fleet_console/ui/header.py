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
    # Диалоги чипов живут в этом контейнере, а не внутри refreshable: иначе таймер refresh сносил бы их через 2 с.
    holder = ui.element("div")

    @ui.refreshable
    def status_row() -> None:
        ks = ctx.kube_status
        ok = ks.allowed and ks.ready and not ks.missing_permissions
        kube_color = "positive" if ok else ("negative" if ks.error else "grey-7")
        _chip(f"{ks.context or 'context?'} · {ctx.settings.namespace}", kube_color, lambda: _kube_dialog(ctx, holder))
        for fwd in ctx.supervisor.forwards:
            st = ctx.supervisor.state(fwd.name)
            _chip(fwd.title, STATUS_COLORS[st.status], lambda name=fwd.name: _forward_dialog(ctx, name, holder))
        guard = ctx.guard
        if guard.reasons:  # чип виден только пока есть что беречь
            ok = guard.active or not guard.supported
            _chip(f"без сна · {len(guard.reasons)}", "positive" if ok else "negative", lambda: _guard_dialog(ctx, holder))
            if guard.battery and not guard.battery.on_ac:
                _chip(f"батарея {guard.battery.percent}%", "warning", lambda: _guard_dialog(ctx, holder))
        ui.link(_cluster_text(ctx), "/cluster").classes("text-white")
        ui.link("Модели", "/models").classes("text-white")
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


def _cluster_text(ctx: AppContext) -> str:
    """Ссылка на дашборд со сводкой: сколько прогонов идёт и сколько GPU занято."""
    obs = ctx.observer
    parts = []
    if obs.runs.data is not None:
        parts.append(f"прогонов идёт: {sum(1 for r in obs.runs.data if r.active)}")
    if obs.mlsc.data is not None:
        parts.append(f"GPU: {sum(v.gpus or 0 for v in obs.mlsc.data)}")
    return "Кластер" + (" · " + " · ".join(parts) if parts else "")


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


def _kube_dialog(ctx: AppContext, holder: ui.element) -> None:
    ks = ctx.kube_status
    with holder, ui.dialog() as dialog, ui.card().classes("min-w-80"):
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
            _kube_dialog(ctx, holder)

        ui.button("Перепроверить", on_click=recheck).props("flat no-caps")
    dialog.open()


def _guard_dialog(ctx: AppContext, holder: ui.element) -> None:
    g = ctx.guard
    with holder, ui.dialog() as dialog, ui.card().classes("min-w-80 max-w-xl"):
        ui.label("Защита от сна").classes("text-lg")
        ui.label(g.status()).classes("text-positive" if g.active else "")
        for text in g.reasons.values():
            ui.label("• " + text).classes("text-sm")
        if g.supported:
            ui.label(shlex.join(g.argv)).classes("font-mono text-xs opacity-70")
        if g.battery:
            percent = f" · {g.battery.percent}%" if g.battery.percent is not None else ""
            if g.battery.on_ac:
                ui.label("питание от сети" + percent).classes("text-sm")
            else:
                ui.label("на батарее: запрет сна слабее, подключите зарядку" + percent).classes("text-sm text-warning")
        ui.label("Закрытую крышку, разряд и ручной сон caffeinate не остановит. Проверка снаружи: pmset -g assertions").classes(
            "text-xs opacity-70"
        )
        ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
    dialog.open()


def _forward_dialog(ctx: AppContext, name: str, holder: ui.element) -> None:
    fwd = ctx.supervisor.forward(name)
    st = ctx.supervisor.state(name)
    with holder, ui.dialog() as dialog, ui.card().classes("min-w-80 max-w-2xl"):
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
