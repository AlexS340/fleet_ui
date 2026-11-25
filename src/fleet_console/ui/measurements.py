"""Личные замеры и ссылки на сводки."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe

from nicegui import ui

from fleet_console.auth import Access, AccessDenied
from fleet_console.context import AppContext
from fleet_console.ui.header import build_header

COLUMNS = [
    {"name": key, "field": key, "label": label, "align": "left"}
    for key, label in (
        ("leaf", "Бенч / агент"), ("score", "Score"), ("succeeded", "Успешно"),
        ("infra_errors", "Ошибки инфраструктуры"), ("timeouts", "Таймауты"), ("unattempted", "Без попытки"),
    )
]


def _digest(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def _require_owner(access: Access, run_id: str) -> None:
    if access.run(run_id).owner_id != access.id:
        raise AccessDenied()


def issue_link(access: Access, run_id: str, hours: int, now: datetime, *, label: str = "") -> str:
    _require_owner(access, run_id)
    if not 1 <= hours <= 24 * 30 or access.store.measurement(run_id) is None:
        raise ValueError("Выберите срок от 1 часа до 30 дней и прогон с замером")
    if len(label.strip()) > 80:
        raise ValueError("Пометка должна быть не длиннее 80 символов")
    token = token_urlsafe(32)
    access.store.add_measurement_link(_digest(token), run_id, (now + timedelta(hours=hours)).isoformat(), label.strip())
    return token


def revoke_link(access: Access, run_id: str, digest: str) -> None:
    _require_owner(access, run_id)
    access.store.revoke_measurement_link(run_id, digest)


def shared_measurement(access: Access, token: str, now: datetime):
    result = access.store.shared_measurement(_digest(token), now.isoformat())
    if result is None:
        raise AccessDenied()
    return result


def summary(row, *, show_run_id: bool = True) -> None:
    if show_run_id:
        ui.label(row["run_id"]).classes("text-lg break-all")
    ui.label(f"SHA fleet {row['sha'] or '—'} · {datetime.fromisoformat(row['measured_at']).astimezone():%d.%m.%Y %H:%M}").classes("text-sm break-all")
    results = json.loads(row["results_json"])
    rows = []
    for result in results:
        completed = result["succeeded"]
        rows.append({
            **{key: result[key] for key in ("leaf", "infra_errors", "timeouts", "unattempted")},
            "score": f"{result['score']:.2f}%",
            "succeeded": f"{completed}/{result['planned']}" if completed is not None else "—",
        })
    ui.table(columns=COLUMNS, rows=rows, row_key="leaf").classes("w-full gt-xs").props("dense flat wrap-cells")
    with ui.column().classes("w-full lt-sm gap-2"):
        for result in rows:
            with ui.card().classes("w-full gap-2"):
                ui.label(result["leaf"]).classes("font-medium break-all")
                for column in COLUMNS[1:]:
                    with ui.row().classes("w-full justify-between gap-1 text-sm"):
                        ui.label(column["label"])
                        ui.label(str(result[column["name"]]))


def measurements_page(ctx: AppContext) -> None:
    build_header(ctx)
    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-3"):
        ui.label("Мои замеры").classes("text-xl")
        results = ctx.store.measurements(ctx.access.id)
        if not results:
            ui.label("Замеров пока нет").classes("text-sm")
        for row in results:
            with ui.expansion(row["run_id"]).classes("w-full"):
                summary(row, show_run_id=False)
                ui.link("Открыть прогон", f"/runs/{row['run_id']}").classes("text-sm")
                if ctx.settings.auth.mode != "local" and row["owner_id"] == ctx.access.id:
                    ui.button("Поделиться", on_click=lambda _, run_id=row["run_id"]: _link_dialog(ctx, run_id)).props("flat no-caps")


def _link_dialog(ctx: AppContext, run_id: str) -> None:
    try:
        _require_owner(ctx.access, run_id)
    except AccessDenied as exc:
        ui.notify(str(exc), type="negative")
        return
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-xl"):
        ui.label(f"Поделиться · {run_id}").classes("text-lg break-all")
        ui.label("Любой, у кого есть ссылка, после входа сможет прочитать сводку этого замера.").classes("text-sm")
        with ui.row().classes("w-full items-start"):
            label = ui.input("Пометка для себя (необязательно)").props("maxlength=80").classes("w-full sm:w-auto sm:grow")
            hours = ui.number("Срок в часах", min=1, max=720, value=24, precision=0).classes("w-28")
        origin = str(ui.context.client.request.base_url).rstrip("/")

        def issue() -> None:
            try:
                token = issue_link(ctx.access, run_id, int(hours.value or 0), datetime.now(UTC), label=label.value)
            except AccessDenied as exc:
                dialog.close()
                ui.notify(str(exc), type="negative")
                return
            except ValueError as exc:
                ui.notify(str(exc), type="negative")
                return
            url.set_value(f"{origin}/measurements/shared/{token}")
            issued.set_visibility(True)
            label.set_value("")
            render()

        def revoke(digest: str) -> None:
            try:
                revoke_link(ctx.access, run_id, digest)
            except AccessDenied as exc:
                dialog.close()
                ui.notify(str(exc), type="negative")
                return
            if url.value and _digest(url.value.rsplit("/", 1)[-1]) == digest:
                issued.set_visibility(False)
                url.set_value("")
            render()

        def render() -> None:
            _require_owner(ctx.access, run_id)
            links.clear()
            with links:
                issued_links = ctx.store.measurement_links(run_id)
                if not issued_links:
                    ui.label("Ссылок пока нет").classes("text-sm")
                for link in issued_links:
                    expires = datetime.fromisoformat(link["expires_at"])
                    revoked = link["revoked_at"] is not None
                    active = not revoked and expires > datetime.now(UTC)
                    with ui.row().classes("w-full items-center justify-between gap-2"):
                        status = "Отозвана" if revoked else ("Действует до" if active else "Истекла")
                        when = datetime.fromisoformat(link["revoked_at"]) if revoked else expires
                        with ui.column().classes("min-w-0 flex-1 gap-0"):
                            ui.label(link["label"] or f"Ссылка {link['digest'][:8]}").classes("break-all")
                            ui.label(f"{status} {when.astimezone():%d.%m.%Y %H:%M}").classes("text-sm")
                        if active:
                            ui.button("Отозвать", on_click=lambda _, digest=link["digest"]: revoke(digest)).props("flat dense no-caps")

        ui.button("Создать ссылку", on_click=issue).props("unelevated no-caps")
        with ui.row().classes("w-full items-center") as issued:
            url = ui.input("Ссылка для отправки").props("readonly").classes("min-w-0 flex-1")
            ui.button("Скопировать", on_click=lambda: ui.clipboard.write(url.value)).props("flat no-caps")
        issued.set_visibility(False)
        ui.separator()
        ui.label("Выданные ссылки").classes("font-medium")
        links = ui.column().classes("w-full gap-2")
        render()
        ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
    dialog.on("hide", dialog.delete)
    dialog.open()
