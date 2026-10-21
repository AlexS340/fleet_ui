"""Страница «Настройки»: поля Settings и кнопка «Сохранить»."""

from __future__ import annotations

from nicegui import ui

from fleet_console.context import AppContext
from fleet_console.ui.header import build_header


def settings_page(ctx: AppContext) -> None:
    build_header(ctx)
    s = ctx.settings
    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-2"):
        ui.label("Настройки").classes("text-xl")
        remote = ui.input("Remote fleet", value=s.fleet_remote).classes("w-full")
        contexts = ui.input("Разрешённые kube context, через запятую", value=", ".join(s.allowed_contexts)).classes("w-full")
        namespace = ui.input("Namespace прогонов", value=s.namespace).classes("w-full")
        ssh_host = ui.input("ssh host бастиона (пусто — сервис не управляет сессией)", value=s.bastion.ssh_host).classes("w-full")
        socks = ui.input("SOCKS-прокси для uv sync", value=s.bastion.socks_proxy).classes("w-full")
        with ui.row().classes("gap-4 flex-wrap"):
            auto_fetch = ui.number("Авто-fetch, минут (0 — выключен)", value=s.auto_fetch_minutes, min=0, precision=0)
            ttl = ui.number("TTL worktree, дней", value=s.worktree_ttl_days, min=1, precision=0)
        ui.label(f"Данные сервиса: {ctx.paths.home}").classes("text-sm opacity-70")

        def save() -> None:
            s.fleet_remote = remote.value.strip()
            s.allowed_contexts = [c.strip() for c in contexts.value.split(",") if c.strip()]
            s.namespace = namespace.value.strip()
            s.bastion.ssh_host = ssh_host.value.strip()
            s.bastion.socks_proxy = socks.value.strip()
            s.auto_fetch_minutes = int(auto_fetch.value or 0)
            s.worktree_ttl_days = int(ttl.value or 1)
            s.save(ctx.paths)
            ui.notify("Сохранено. Пробросы и зеркало применят новые значения после перезапуска сервиса.")

        ui.button("Сохранить", on_click=save).props("unelevated no-caps")
