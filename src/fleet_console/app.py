"""Точка входа Fleet Console: страницы NiceGUI и запуск сервера."""

from __future__ import annotations

from datetime import datetime, timezone

from nicegui import app, ui
from starlette.responses import PlainTextResponse

from fleet_console.auth import AccessDenied, IdentityAdapter, IdentityMiddleware, TestIdentityAdapter, install_socket_guard
from fleet_console.context import AppContext, build_context
from fleet_console.settings import read_secret
from fleet_console.ui.admin import admin_page
from fleet_console.ui.benches import benches_page
from fleet_console.ui.cluster import cluster_page
from fleet_console.ui.header import profile_control
from fleet_console.ui.models import models_page
from fleet_console.ui.measurements import measurements_page, shared_measurement, summary
from fleet_console.ui.revision import revision_page
from fleet_console.ui.settings import settings_page
from fleet_console.store.db import LOCAL_OWNER


def create_app(ctx: AppContext, identity_adapter: IdentityAdapter | None = None) -> None:
    """Зарегистрировать страницы и хуки жизненного цикла для данного контекста."""

    auth = ctx.settings.auth
    if auth.mode != "local":
        if auth.mode == "test":
            adapter = identity_adapter if isinstance(identity_adapter, TestIdentityAdapter) else TestIdentityAdapter.from_env(
                read_secret(ctx.paths, "FLEET_CONSOLE_TEST_USERS") or "{}"
            )
            adapter.bind(ctx.store)
            app.add_api_route("/test-login", adapter.login, methods=["GET", "POST"], include_in_schema=False)
            app.add_api_route("/test-logout", adapter.logout, methods=["POST"], include_in_schema=False)
            identity_adapter = adapter
        elif isinstance(identity_adapter, TestIdentityAdapter):
            raise ValueError("Тестовый адаптер доступен в режиме test")
        for user_id in auth.admins:
            if user_id and user_id != LOCAL_OWNER:
                ctx.store.register_user(user_id, admin=True)
        if auth.legacy_owner.strip() and auth.legacy_owner != LOCAL_OWNER:
            ctx.store.register_user(auth.legacy_owner)
            ctx.store.migrate_owner(auth.legacy_owner)
            ctx.local_datasets.migrate_owner(auth.legacy_owner)
        install_socket_guard()
        app.add_middleware(IdentityMiddleware, adapter=identity_adapter, store=ctx.store)

    @ui.page("/")
    async def index() -> None:
        await revision_page(ctx)

    @ui.page("/cluster")
    async def cluster() -> None:
        await cluster_page(ctx)

    @ui.page("/models")
    async def models() -> None:
        await models_page(ctx)

    @ui.page("/models/{session_id}")
    async def model(session_id: str):
        try:
            ctx.access.session(session_id)
        except AccessDenied:
            return PlainTextResponse("Объект недоступен", status_code=404)
        await models_page(ctx, session_id=session_id)

    @ui.page("/runs")
    async def runs() -> None:
        await benches_page(ctx)

    @ui.page("/runs/{run_id}")
    async def run(run_id: str):
        try:
            ctx.access.run(run_id)
        except AccessDenied:
            return PlainTextResponse("Объект недоступен", status_code=404)
        await benches_page(ctx, run_id=run_id)

    @ui.page("/measurements")
    def measurements() -> None:
        measurements_page(ctx)

    @ui.page("/measurements/shared/{token}")
    def share(token: str):
        try:
            if auth.mode == "local":
                raise AccessDenied()
            result = shared_measurement(ctx.access, token, datetime.now(timezone.utc))
        except AccessDenied:
            return PlainTextResponse("Ссылка недоступна", status_code=404)
        with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-3"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Сводка замера").classes("text-xl")
                profile_control(ctx)
            summary(result)

    @ui.page("/settings")
    def settings():
        if not ctx.access.admin:
            return PlainTextResponse("Требуются права администратора", status_code=403)
        settings_page(ctx)

    @ui.page("/admin")
    def admin():
        if not ctx.access.admin:
            return PlainTextResponse("Требуются права администратора", status_code=403)
        admin_page(ctx)

    app.on_startup(ctx.startup)
    app.on_shutdown(ctx.shutdown)


def main() -> None:
    """Запустить UI на 127.0.0.1:<ui_port>; тема следует за системной."""
    ctx = build_context()
    create_app(ctx)
    ui.run(
        host="127.0.0.1", port=ctx.settings.ui_port, title="Fleet Console", reload=False, show=False, dark=None,
        access_log=ctx.settings.auth.mode == "local",
    )


if __name__ == "__main__":
    main()
