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
from fleet_console.observe.submitter import ROOT
from fleet_console.ui.common import confirm_dialog, hint
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
    _col("own", "Свой", "left"),
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
    _col("own", "Свой", "left"),
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


HINTS = {
    "Прогоны Argo": (
        "Argo Workflows — движок задач в кластере. Прогон — запуск бенча: пачка workflows (по одному на задачу) "
        "с общим run-name. Усыновить — пометить чужой прогон своим, чтобы управлять им отсюда."
    ),
    "Модели MLSC": "MLSC — сервис инференса. Версия — поднятая модель; план и факт — сколько инстансов заказано и живёт.",
    "Шлюз nginx": "nginx перед моделями: маршрут по имени модели, реплики — сколько инстансов в нём.",
    "Сабмиттер": "Под, который по очереди шлёт workflows прогона в Argo. Лимит агентов — сколько задач идёт одновременно; 0 — пауза.",
    "Узлы пула": "Узлы type=bench: на них бегут задачи прогонов.",
}


class _Card:
    """Карточка источника: заголовок с подсказкой, время опроса, ошибка, сводка, таблица, подпись."""

    def __init__(self, source: Source, columns: list[dict] | None = None, row_key: str = "id") -> None:
        self.source = source
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-baseline gap-2"):
                ui.label(source.title).classes("text-lg")
                if source.title in HINTS:
                    hint(HINTS[source.title])
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
        runs.table.on("rowClick", lambda e: _run_dialog(ctx, e.args[1], render))
        mlsc = _Card(obs.mlsc, MLSC_COLUMNS, "id")
        route = _Card(obs.nginx, NGINX_COLUMNS, "model")
        sub = _Card(obs.submitter, SUBMITTER_COLUMNS, "run")
        sub.table.on("rowClick", lambda e: _log_dialog(ctx, e.args[1]["run"]))
        nodes = _Card(obs.nodes)
    cards = (runs, mlsc, route, sub, nodes)

    def render() -> None:
        state["version"] = obs.version
        _render_runs(runs, ctx, state["only_active"])
        _render_mlsc(mlsc, ctx)
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
    own = ctx.store.own_runs()
    rows = [
        {
            "run": r.run_name,
            "model": r.model or "—",
            "own": "да" if r.run_name in own else "—",
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
    seen = {r.run_name for r in all_runs}
    rows += [  # свой прогон, чьих workflows в Argo уже нет (удалены или ушли в архив): иначе его негде «забыть»
        {
            "run": o.run_id,
            "model": o.model or "—",
            "own": "да · workflows не видно",
            "running": 0,
            "pending": 0,
            "succeeded": 0,
            "failed": 0,
            "started": "—",
            "finished": "—",
            "argo": argo_ui_url(base, ctx.settings.namespace, o.run_id),
        }
        for o in own.values()
        if o.run_id not in seen
    ]
    summary = "" if rows else f"активных прогонов нет (всего {len(all_runs)})"
    note = "клик по строке — усыновить чужой прогон или забыть свой"
    if len(shown) < len(all_runs):
        note += f" · скрыто завершённых прогонов: {len(all_runs) - len(shown)}"
    card.render(rows, summary, note)


def _render_mlsc(card: _Card, ctx: AppContext) -> None:
    if card.source.data is None:
        card.render()
        return
    versions = card.source.data
    own = ctx.store.own_runs().values()
    rows = [
        {
            "id": v.version_id,
            "application": v.application,
            "model": v.model or "—",
            "own": "да" if any(o.owns_version(v.version_id, v.application) for o in own) else "—",
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


def _run_dialog(ctx: AppContext, row: dict, on_change) -> None:  # noqa: ANN001 — колбэк перерисовки страницы
    """Чужой прогон — усыновить (run_id, version_id MLSC, заметка); свой — забыть."""
    run_id = row["run"]
    own = ctx.store.get_run(run_id)
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-xl"):
        ui.label(run_id).classes("text-lg break-all")
        if own:
            _own_controls(ctx, dialog, own, row, on_change)
        else:
            ui.label("Чужой прогон: запущен не из сервиса. Усыновлённым можно управлять (с Ф2).").classes("text-sm opacity-80")
            suggested = next((v.version_id for v in ctx.observer.mlsc.data or [] if run_id in v.application), None)
            version = ui.number("version_id MLSC (необязательно)", value=suggested, precision=0, min=0).classes("w-full")
            ui.label("version_id — в карточке «Модели MLSC» у версии, чьё приложение содержит run_id").classes("text-xs opacity-60")
            note = ui.input("Заметка").classes("w-full")

            def adopt() -> None:
                version_id = int(version.value) if version.value else None
                ctx.store.adopt_run(run_id, version_id, model=row.get("model", ""), note=note.value.strip())
                ctx.store.event("adopt_run", run_id, "ok", f"version_id={version_id}")
                dialog.close()
                on_change()
                ui.notify(f"{run_id}: теперь свой")

            ui.button("Усыновить", on_click=adopt).props("unelevated no-caps")
    dialog.open()


def _own_controls(ctx: AppContext, dialog: ui.dialog, own, row: dict, on_change) -> None:
    """Свой прогон: лимит агентов, реплики модели, остановка, «забыть». Всё разрушительное — через подтверждение."""
    run_id = own.run_id
    version = next((v for v in ctx.observer.mlsc.data or [] if own.owns_version(v.version_id, v.application)), None)
    sub = next((s for s in ctx.observer.submitter.data or [] if s.run_id == run_id), None)
    adopted = _when(datetime.fromisoformat(own.adopted_at))
    facts = ["свой", f"усыновлён {adopted}", f"задач идёт {row.get('running', 0)}, в очереди {row.get('pending', 0)}"]
    ui.label(" · ".join(facts + ([own.note] if own.note else []))).classes("text-sm")

    with ui.row().classes("items-end gap-2 flex-wrap"):
        limit = ui.number("Лимит агентов", value=sub.limit if sub and sub.limit is not None else 0, min=0, precision=0).classes("w-40")

        def ask_limit() -> None:
            n = int(limit.value or 0)
            _confirm(
                f"Записать лимит агентов {n} для {run_id}",
                [f"файл {ROOT}/{run_id}/agents_limit на поде {ctx.settings.cluster.submitter_pod}", "0 — пауза: сабмиттер перестаёт слать задачи"],
                lambda: ctx.set_agent_limit(run_id, n),
                on_change,
            )

        ui.button("Записать", on_click=ask_limit).props("flat no-caps")
    if sub:
        ui.label(
            f"на поде: лимит {sub.limit if sub.limit is not None else '—'}, в очереди {sub.pending}, сабмиттер {'жив' if sub.alive else 'не запущен'}"
        ).classes("text-xs opacity-70")
    else:
        ui.label("каталога прогона на submitter-поде не видно").classes("text-xs opacity-70")

    if version:
        gpu = version.gpu_per_instance or 0
        with ui.row().classes("items-end gap-2 flex-wrap"):
            replicas = ui.number("Реплики модели", value=version.planned, min=0, precision=0).classes("w-40")

            def ask_replicas() -> None:
                n = int(replicas.value or 0)
                _confirm(
                    f"Версии {version.version_id} выставить {n} инстансов",
                    [f"сейчас план {version.planned}, факт {version.current}", f"{gpu} GPU на инстанс → {n * gpu} GPU", "0 — погасить модель"],
                    lambda: ctx.set_replicas(version.version_id, n),
                    on_change,
                )

            ui.button("Применить", on_click=ask_replicas).props("flat no-caps")
        ui.label(f"MLSC версия {version.version_id}: план {version.planned}, факт {version.current}, {version.instance_type or '?'}").classes(
            "text-xs opacity-70"
        )
    else:
        hint = f"version_id {own.version_id}" if own.version_id is not None else "version_id не указан"
        ui.label(f"живой версии MLSC у прогона не видно ({hint})").classes("text-xs opacity-70")

    def forget() -> None:
        ctx.store.forget_run(run_id)
        ctx.store.event("forget_run", run_id, "ok")
        dialog.close()
        on_change()
        ui.notify(f"{run_id}: больше не свой")

    with ui.row().classes("gap-2 mt-2"):
        ui.button("Остановить прогон", on_click=lambda: _stop_dialog(ctx, run_id, row, version, on_change)).props("flat no-caps color=negative")
        ui.button("Забыть", on_click=forget).props("flat no-caps")


_confirm = confirm_dialog


def _stop_dialog(ctx: AppContext, run_id: str, row: dict, version, on_change) -> None:
    active = int(row.get("running", 0) or 0) + int(row.get("pending", 0) or 0)
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-xl"):
        ui.label(f"Остановить прогон {run_id}?").classes("text-lg break-all")
        ui.label(
            f"Активных задач: {active}. Лимит агентов станет 0, сабмиттер прогона будет убит, активным workflows уйдёт Terminate."
        ).classes("text-sm")
        stop_model = None
        if version:
            gpu = (version.gpu_per_instance or 0) * version.planned
            stop_model = ui.checkbox(
                f"погасить модель: версия {version.version_id}, план {version.planned} × {version.gpu_per_instance or '?'} GPU = {gpu} GPU", value=True
            )
        else:
            ui.label("Живой модели прогона в MLSC нет — гасить нечего.").classes("text-sm opacity-70")
        log = ui.label().classes("text-sm whitespace-pre-wrap font-mono")

        async def run() -> None:
            log.set_text("останавливаю…")
            version_id = version.version_id if version and stop_model and stop_model.value else None
            report = await ctx.stop_own_run(run_id, version_id)
            lines = ["✓ " + s for s in report.steps] + ["✗ " + f for f in report.failed]
            log.set_text("\n".join(lines))
            log.classes(add="text-negative" if report.failed else "", remove="" if report.failed else "text-negative")
            on_change()

        with ui.row().classes("gap-2"):
            ui.button("Остановить", on_click=run).props("unelevated no-caps color=negative")
            ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
    dialog.open()


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
