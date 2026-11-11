"""Личные замеры и ссылки на сводки."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
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


def issue_link(access: Access, run_id: str, hours: int, now: datetime) -> str:
    access.run(run_id)
    if not 1 <= hours <= 24 * 30 or access.store.measurement(run_id) is None:
        raise ValueError("Выберите срок от 1 часа до 30 дней и прогон с замером")
    token = token_urlsafe(32)
    access.store.add_measurement_link(_digest(token), run_id, (now + timedelta(hours=hours)).isoformat())
    return token


def revoke_link(access: Access, run_id: str, digest: str) -> None:
    access.run(run_id)
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
            **result, "score": f"{result['score']:.2f}%",
            "succeeded": f"{completed}/{result['planned']}" if completed is not None else "—",
        })
    ui.table(columns=COLUMNS, rows=rows, row_key="leaf").classes("w-full hidden sm:block").props("dense flat wrap-cells")
    with ui.column().classes("w-full sm:hidden gap-2"):
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
                if ctx.settings.auth.mode != "local":
                    ui.button("Поделиться сводкой", on_click=lambda _, run_id=row["run_id"]: _link_dialog(ctx, run_id)).props("flat no-caps")


def _link_dialog(ctx: AppContext, run_id: str) -> None:
    ctx.access.run(run_id)
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-xl"):
        ui.label("Поделиться сводкой").classes("text-lg")
        ui.label("Ссылка открывает сводку вошедшему пользователю.").classes("text-sm")
        hours = ui.number("Срок действия, часов", min=1, max=720, value=24, precision=0)
        url = ui.input("Ссылка").props("readonly").classes("w-full")
        url.set_visibility(False)
        links = ui.column().classes("w-full gap-1")
        origin = str(ui.context.client.request.base_url).rstrip("/")

        def issue() -> None:
            try:
                token = issue_link(ctx.access, run_id, int(hours.value or 0), datetime.now(timezone.utc))
            except (ValueError, AccessDenied) as exc:
                ui.notify(str(exc), type="negative")
                return
            url.set_value(f"{origin}/measurements/shared/{token}")
            url.set_visibility(True)
            render()

        def revoke(digest: str) -> None:
            revoke_link(ctx.access, run_id, digest)
            if url.value and _digest(url.value.rsplit("/", 1)[-1]) == digest:
                url.set_visibility(False)
                url.set_value("")
            render()

        def render() -> None:
            ctx.access.run(run_id)
            links.clear()
            with links:
                for link in ctx.store.measurement_links(run_id):
                    expires = datetime.fromisoformat(link["expires_at"])
                    revoked = link["revoked_at"] is not None
                    active = not revoked and expires > datetime.now(timezone.utc)
                    with ui.row().classes("items-center"):
                        status = "отозвана" if revoked else ("действует до" if active else "истекла")
                        ui.label(f"{status} {expires.astimezone():%d.%m.%Y %H:%M} · {link['digest'][:8]}").classes("text-sm")
                        if active:
                            ui.button("Отозвать", on_click=lambda _, digest=link["digest"]: revoke(digest)).props("flat dense no-caps")

        ui.button("Создать ссылку", on_click=issue).props("unelevated no-caps")
        render()
        ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
    dialog.open()
