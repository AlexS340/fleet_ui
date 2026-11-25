"""Управление аккаунтами и принадлежностью прогонов."""

from nicegui import ui

from fleet_console.auth import AccessDenied, hash_new_password
from fleet_console.context import AppContext
from fleet_console.store.db import LOCAL_OWNER
from fleet_console.ui.header import build_header


def _account_options(ctx: AppContext) -> dict[str, str]:
    return {
        user.id: (f"{user.display_name} · {user.id}" if user.display_name else user.id)
        + (" · ожидает одобрения" if user.pending_approval else " · отключён" if not user.enabled else "")
        for user in ctx.store.users()
    }


def admin_page(ctx: AppContext) -> None:
    ctx.access.require_admin()
    build_header(ctx)
    labels = {"admin_user": "Настройки пользователя", "admin_password": "Сброс пароля", "admin_run": "Владелец и соавторы"}

    @ui.refreshable
    def audit() -> None:
        ctx.access.require_admin()
        events = ctx.store.admin_events(limit=20)
        if not events:
            ui.label("Изменений пока нет").classes("text-sm")
        for event in events:
            ui.label(f"{event.ts[:16].replace('T', ' ')} UTC · {event.actor_id} · "
                     f"{labels[event.action]} · {event.object}").classes("text-sm break-all")

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-3"):
        ui.label("Админка").classes("text-xl")
        with ui.tabs().classes("w-full").props("align=left no-caps") as tabs:
            users = ui.tab("Пользователи")
            runs = ui.tab("Прогоны")
        with ui.tab_panels(tabs, value=users).classes("w-full"):
            with ui.tab_panel(users).classes("p-0"):
                _users_panel(ctx, audit.refresh)
            with ui.tab_panel(runs).classes("p-0"):
                _runs_panel(ctx, audit.refresh)
        with ui.expansion("Журнал изменений").classes("w-full"):
            audit()


def _limit(value: float | None) -> int | None:
    if value is None:
        return None
    if not 1 <= value <= 1_000_000 or value != int(value):
        raise ValueError("Введите целую квоту от 1 до 1 000 000 или оставьте поле пустым")
    return int(value)


def _users_panel(ctx: AppContext, changed) -> None:
    accounts = _account_options(ctx)
    if not accounts:
        ui.label("Учётных записей пока нет. Они появятся после настройки входа или первого входа пользователя.").classes("text-sm")
        return
    select = ui.select(accounts, label="Пользователь", with_input=True).classes("w-full")

    @ui.refreshable
    def editor() -> None:
        access = ctx.access
        access.require_admin()
        account = ctx.store.user(select.value) if select.value else None
        if account is None:
            return
        with ui.card().classes("w-full gap-3"):
            name = ui.input("Отображаемое имя", value=account.display_name).props("maxlength=100").classes("w-full")
            role = ui.select({"user": "Пользователь", "admin": "Администратор"}, label="Роль", value=account.role).classes("w-full")
            enabled = ui.switch("Разрешить вход", value=bool(account.enabled))
            if account.id == access.id:
                role.disable()
                enabled.disable()
            quotas = ctx.settings.quotas
            with ui.element("div").classes("w-full grid grid-cols-1 sm:grid-cols-2 gap-3"):
                run_limit = ui.number("Лимит заявок", value=account.run_limit, min=1).props(f"placeholder={quotas.user_runs}")
                gpu_limit = ui.number("Лимит GPU", value=account.gpu_limit, min=1).props(f"placeholder={quotas.user_gpus}")
            ui.label("Пустое поле наследует общий лимит на пользователя. Изменение квоты не останавливает текущие прогоны.").classes("text-xs opacity-70")

            def save() -> None:
                try:
                    access = ctx.access
                    access.require_admin()
                    ctx.store.update_user(access.id, account.id, role=role.value, enabled=enabled.value,
                                          display_name=name.value.strip(), run_limit=_limit(run_limit.value),
                                          gpu_limit=_limit(gpu_limit.value))
                except (AccessDenied, ValueError) as exc:
                    ui.notify(str(exc), type="negative")
                    return
                select.set_options(_account_options(ctx), value=account.id)
                changed()
                ui.notify("Настройки пользователя сохранены", type="positive")

            with ui.row().classes("gap-2"):
                ui.button("Сохранить пользователя", on_click=save).props("unelevated no-caps")
                if ctx.settings.auth.mode == "test":
                    caption = "Сбросить пароль" if account.has_password else "Задать пароль"
                    ui.button(caption, on_click=lambda: _password_dialog(ctx, account.id, after_reset)).props("flat no-caps")
            if ctx.settings.auth.mode == "shared":
                ui.label("Пароли управляются во внешней системе входа.").classes("text-sm opacity-70")

    def after_reset() -> None:
        editor.refresh()
        changed()

    select.on_value_change(lambda: editor.refresh())
    editor()


def _password_dialog(ctx: AppContext, user_id: str, changed) -> None:
    ctx.access.require_admin()
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
        ui.label("Сброс пароля").classes("text-lg")
        ui.label(user_id).classes("break-all")
        ui.label("Новый пароль вступит в силу сразу. Пользователю нужно будет войти заново.").classes("text-sm")
        password = ui.input("Новый пароль", password=True, password_toggle_button=True).props(
            'autocomplete=new-password maxlength=256'
        ).classes("w-full")
        ui.label("От 12 до 256 символов").classes("text-xs opacity-70")
        error = ui.label().classes("text-negative text-sm")

        async def reset() -> None:
            confirm.disable()
            try:
                ctx.access.require_admin()
                digest = await hash_new_password(password.value)
                access = ctx.access
                access.require_admin()
                ctx.store.reset_password(access.id, user_id, digest)
            except (AccessDenied, ValueError) as exc:
                error.set_text(str(exc))
                return
            finally:
                confirm.enable()
            password.set_value("")
            dialog.close()
            if access.id == user_id:
                ui.navigate.to("/test-login")
            else:
                changed()
                ui.notify("Пароль изменён. Старые сеансы отозваны", type="positive")

        with ui.row().classes("gap-2"):
            confirm = ui.button("Установить новый пароль", on_click=reset).props("unelevated no-caps")
            ui.button("Отмена", on_click=dialog.close).props("flat no-caps")
        dialog.on("hide", lambda: password.set_value(""))
    dialog.open()


def _runs_panel(ctx: AppContext, changed) -> None:
    benches = {run.id: run for run in ctx.store.benches()}
    run_ids = [*benches, *(run_id for run_id in ctx.store.own_runs() if run_id not in benches)]
    if not run_ids:
        ui.label("Зарегистрированных прогонов пока нет").classes("text-sm")
        return
    select = ui.select(run_ids, label="Прогон", with_input=True).classes("w-full")

    @ui.refreshable
    def editor() -> None:
        ctx.access.require_admin()
        run_id = select.value
        ownership = ctx.store.run_ownership(run_id) if run_id else None
        if ownership is None:
            return
        accounts = _account_options(ctx)
        current, authors = ownership
        owner_options = {**accounts}
        if current not in owner_options:
            owner_options[current] = "Локальный владелец" if current == LOCAL_OWNER else current
        with ui.card().classes("w-full gap-3"):
            owner = ui.select(owner_options, label="Владелец", value=current, with_input=True).classes("w-full")
            coauthors = ui.select(accounts, label="Соавторы", value=authors, multiple=True, with_input=True, clearable=True).props("use-chips").classes("w-full")
            ui.label("Владелец и соавторы видят прогон и замер и могут управлять прогоном. "
                     "При удалении участника старые ссылки на замер отзываются.").classes("text-sm")
            ui.label("Квота и сессия модели остаются за исходной заявкой.").classes("text-xs opacity-70")

            def save() -> None:
                try:
                    access = ctx.access
                    access.require_admin()
                    ctx.store.set_run_ownership(access.id, run_id, owner.value, coauthors.value or [])
                except (AccessDenied, ValueError) as exc:
                    ui.notify(str(exc), type="negative")
                    return
                editor.refresh()
                changed()
                ui.notify("Принадлежность прогона сохранена", type="positive")

            with ui.row().classes("items-center gap-3"):
                ui.button("Сохранить принадлежность", on_click=save).props("unelevated no-caps")
                if run_id in benches:
                    ui.link("Открыть прогон", f"/runs/{run_id}")

    select.on_value_change(lambda: editor.refresh())
    editor()
