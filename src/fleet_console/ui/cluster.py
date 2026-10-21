"""Страница «Кластер»: прогоны Argo, модели MLSC, шлюз nginx, сабмиттер, узлы пула — только чтение.

Карточки строятся один раз, по новому опросу обновляются на месте (`update_rows`): так не слетают
сортировка таблиц и открытый диалог лога.
"""

from __future__ import annotations

from datetime import datetime, timezone

from nicegui import ui

from fleet_console.context import AppContext
from fleet_console.observe import Source
from fleet_console.observe.argo import argo_ui_url
from fleet_console.ui.header import build_header

LINK_SLOT = '<q-td :props="props"><a :href="props.value" target="_blank" class="text-primary">Argo UI</a></q-td>'


def _ago(at: datetime | None) -> str:
    if at is None:
        return "ещё не опрашивалось"
    s = int((datetime.now(timezone.utc) - at).total_seconds())
    return f"обновлено {s} с назад" if s < 120 else f"обновлено {s // 60} мин назад"


def _when(at: datetime | None) -> str:
    return at.astimezone().strftime("%d.%m %H:%M") if at else "—"


def _col(name: str, label: str, align: str = "right") -> dict:
    return {"name": name, "label": label, "field": name, "align": align, "sortable": True}


RUN_COLUMNS = [
    _col("run", "Прогон", "left"),
    _col("model", "Модель", "left"),
    _col("running", "Идут"),
    _col("pending", "В очереди"),
    _col("succeeded", "Успешно"),
    _col("failed", "Упали"),
    _col("started", "Начат", "left"),
    _col("finished", "Последнее завершение", "left"),
    {"name": "argo", "label": "", "field": "argo"},
]
MLSC_COLUMNS = [
    _col("id", "version_id", "left"),
    _col("application", "Приложение", "left"),
    _col("model", "Модель", "left"),
    _col("planned", "План"),
    _col("current", "Факт"),
    _col("instance", "Тип инстанса", "left"),
    _col("gpus", "GPU"),
]
NGINX_COLUMNS = [_col("model", "Модель", "left"), _col("replicas", "Реплик в маршруте")]
SUBMITTER_COLUMNS = [
    _col("run", "Прогон", "left"),
    _col("limit", "Лимит агентов"),
    _col("pending", "В очереди"),
    _col("submitted", "Отправлено"),
    _col("completed", "Завершено"),
    _col("alive", "Сабмиттер", "left"),
    _col("log", "Лог обновлён", "left"),
]


class _Card:
    """Карточка источника: заголовок, время опроса, ошибка, сводка, таблица, подпись."""

    def __init__(self, source: Source, columns: list[dict] | None = None, row_key: str = "id") -> None:
        self.source = source
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-baseline gap-2"):
                ui.label(source.title).classes("text-lg")
                self.ago = ui.label().classes("text-xs opacity-60")
            self.error = ui.label().classes("text-negative text-sm whitespace-pre-wrap font-mono")
            self.summary = ui.label().classes("text-sm opacity-80")
            self.table = None
            if columns:
                self.table = ui.table(columns=columns, rows=[], row_key=row_key).classes("w-full").props("dense flat")
                self.table.set_visibility(False)
            self.note = ui.label().classes("text-xs opacity-60")

    def render(self, rows: list[dict] | None = None, summary: str = "", note: str = "") -> None:
        self.ago.set_text(_ago(self.source.at))
        _set(self.error, self.source.error)
        _set(self.summary, summary)
        _set(self.note, note)
        if self.table is not None:
            self.table.update_rows(rows or [])
            self.table.set_visibility(bool(rows))


def _set(label: ui.label, text: str) -> None:
    label.set_text(text)
    label.set_visibility(bool(text))


async def cluster_page(ctx: AppContext) -> None:
    build_header(ctx)
    obs = ctx.observer
    state = {"version": -1, "only_active": True}

    async def refresh() -> None:
        if obs.busy:
            ui.notify("Опрос уже идёт")
            return
        await obs.refresh()
        render()

    def toggle(value: bool) -> None:
        state["only_active"] = value
        render()

    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            ui.label("Кластер").classes("text-xl")
            ui.space()
            ui.switch("только активные", value=True, on_change=lambda e: toggle(e.value))
            ui.button("Обновить", on_click=refresh).props("flat no-caps")
        runs = _Card(obs.runs, RUN_COLUMNS, "run")
        runs.table.add_slot("body-cell-argo", LINK_SLOT)
        mlsc = _Card(obs.mlsc, MLSC_COLUMNS, "id")
        route = _Card(obs.nginx, NGINX_COLUMNS, "model")
        sub = _Card(obs.submitter, SUBMITTER_COLUMNS, "run")
        sub.table.on("rowClick", lambda e: _log_dialog(ctx, e.args[1]["run"]))
        nodes = _Card(obs.nodes)
    cards = (runs, mlsc, route, sub, nodes)

    def render() -> None:
        state["version"] = obs.version
        _render_runs(runs, ctx, state["only_active"])
        _render_mlsc(mlsc)
        _render_nginx(route)
        _render_submitter(sub, state["only_active"])
        _render_nodes(nodes)

    def tick() -> None:
        if obs.version != state["version"]:
            render()
        else:
            for card in cards:
                card.ago.set_text(_ago(card.source.at))

    render()
    ui.timer(2.0, tick)


def _render_runs(card: _Card, ctx: AppContext, only_active: bool) -> None:
    if card.source.data is None:
        card.render()
        return
    all_runs = card.source.data
    shown = [r for r in all_runs if r.active] if only_active else all_runs
    base = ctx.settings.argo_ui_url.rstrip("/")
    rows = [
        {
            "run": r.run_name,
            "model": r.model or "—",
            "running": r.running,
            "pending": r.pending,
            "succeeded": r.succeeded,
            "failed": r.failed,
            "started": _when(r.started),
            "finished": _when(r.last_finished),
            "argo": argo_ui_url(base, ctx.settings.namespace, r.run_name),
        }
        for r in shown
    ]
    summary = "" if shown else f"активных прогонов нет (всего {len(all_runs)})"
    note = f"скрыто завершённых прогонов: {len(all_runs) - len(shown)}" if len(shown) < len(all_runs) else ""
    card.render(rows, summary, note)


def _render_mlsc(card: _Card) -> None:
    if card.source.data is None:
        card.render()
        return
    versions = card.source.data
    rows = [
        {
            "id": v.version_id,
            "application": v.application,
            "model": v.model or "—",
            "planned": v.planned,
            "current": v.current,
            "instance": v.instance_type or "—",
            "gpus": v.gpus if v.gpus is not None else "?",
        }
        for v in versions
    ]
    card.render(rows, f"живых версий: {len(versions)} · GPU занято: {sum(v.gpus or 0 for v in versions)}")


def _render_nginx(card: _Card) -> None:
    if card.source.data is None:
        card.render()
        return
    rows = [{"model": m, "replicas": n} for m, n in sorted(card.source.data.items())]
    card.render(rows, "" if rows else "в маршруте нет ни одной модели")


def _render_submitter(card: _Card, only_active: bool) -> None:
    if card.source.data is None:
        card.render()
        return
    all_runs = card.source.data
    shown = [r for r in all_runs if r.alive] if only_active else all_runs  # на поде сотни каталогов старых прогонов
    rows = [
        {
            "run": r.run_id,
            "limit": r.limit if r.limit is not None else "—",
            "pending": r.pending,
            "submitted": r.submitted,
            "completed": r.completed,
            "alive": "жив" if r.alive else "нет",
            "log": _when(r.log_at),
        }
        for r in shown
    ]
    summary = "" if shown else f"живых сабмиттеров нет (каталогов прогонов: {len(all_runs)})"
    note = "клик по строке — хвост run_logs.txt" if shown else ""
    if len(shown) < len(all_runs):
        note += f" · скрыто каталогов без живого сабмиттера: {len(all_runs) - len(shown)}"
    card.render(rows, summary, note)


def _render_nodes(card: _Card) -> None:
    if card.source.data is None:
        card.render()
        return
    p = card.source.data
    facts = [
        f"узлов {p.total}, готовых {p.ready}" + (f", cordoned {p.unschedulable}" if p.unschedulable else ""),
        f"CPU allocatable {p.cpu_cores:.0f}",
        f"память {p.memory_gib:.0f} GiB",
        f"подов в Pending: {p.pending_pods}",
    ]
    card.render(summary=" · ".join(facts))


async def _log_dialog(ctx: AppContext, run_id: str) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label(f"run_logs.txt · {run_id}").classes("text-lg")
        log = ui.label("читаю…").classes("font-mono text-xs whitespace-pre-wrap w-full")
    dialog.open()
    try:
        log.set_text(await ctx.observer.submitter_log(run_id) or "(пусто)")
    except Exception as exc:  # noqa: BLE001 — ошибку kubectl показываем в диалоге
        log.set_text(str(exc))
        log.classes("text-negative")
