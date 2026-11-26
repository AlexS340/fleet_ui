"""Страница «Прогоны»: форма запуска бенча из пресета, превью «что и где» и preflight, карточка прогона.

Одно главное действие — «Запустить бенч». Детали по раскрытию: форма → «Дополнительно» (overrides,
harbor_job) → «Полный конфиг» (exp.yaml, файлы defaults с комментариями и правкой, resolved.yaml).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import shutil
from typing import Any
from uuid import uuid4

from nicegui import ui
import yaml

from fleet_console.auth import AccessDenied
from fleet_console.compose import (
    BenchSpec,
    Check,
    Drift,
    Preview,
    SourceFile,
    build_registry,
    copy_for_edit,
    from_resolved,
    load_issues,
    make_bench_id,
    manual_edits,
    overlay_origin,
    plan_fetch,
    run_checks,
    save_to_overlay,
    sources,
    summary,
    tasks_total,
    three_way,
    validate_yaml,
)
from fleet_console.compose.local_datasets import LocalDataset
from fleet_console.compose.model_route import model_route_problem
from fleet_console.compose.policy import private_root, supports_private_measure
from fleet_console.compose.task_issues import task_issue_checks
from fleet_console.compose.provenance import Candidate, build_index, layers, lookup, tree_nodes, value_at
from fleet_console.compose.registry import Mapping, bench_slug, group_tasks_roots, list_mappings, registry_choices
from fleet_console.compose.task_resources import task_resources
from fleet_console.compose.reuse import config_path, preset_from_run
from fleet_console.context import AppContext
from fleet_console.fleetref.manager import PrepareError, ReadyRevision
from fleet_console.fleetref.profile import CONFIGS_REL, list_groups
from fleet_console.models.compose import recipe_summary
from fleet_console.observe.argo import TaskWorkflow, argo_ui_url, workflow_url
from fleet_console.observe.s3 import S3Object, TaskResult, failed_selection, scan_results
from fleet_console.presets import DatasetRef, Preset, compatibility, load_presets, personal_dir, save_personal
from fleet_console.runs import STAGE_TITLES, BenchError, dataset_of
from fleet_console.settings import OVERLAY_DIR, PRESETS_DIR, scrub
from fleet_console.store import BenchRun
from fleet_console.store.db import LOCAL_OWNER
from fleet_console.ui.common import JOB_PRIORITIES, JOB_PRIORITY_OVERRIDE, JOB_QUEUE_KEY, JOB_QUEUES, confirm_dialog, hint, job_options_select, set_job_priority_options, split_job_options, text_dialog
from fleet_console.ui.header import build_header
from fleet_console.ui.local_datasets import local_panel

STATUS_TEXT = {"queued": "в очереди", "running": "идёт", "attention": "требует внимания", "stopping": "останавливается", "stopped": "остановлен", "failed": "не удался", "done": "готов"}
HIDDEN_STATUS_OPTIONS = {
    "failed": "Неудачные и с ошибками", "stopped": "Остановленные", "running": "Выполняющиеся",
    "queued": "В очереди", "attention": "Требуют внимания", "stopping": "Останавливаются", "done": "Успешные",
}
LEVEL_ICON = {"ok": ("check_circle", "text-positive"), "warn": ("warning", "text-warning"), "fail": ("cancel", "text-negative")}
COLUMNS = [
    {"name": "id", "label": "Прогон", "field": "id", "align": "left", "sortable": True,
     "style": "white-space: normal; overflow-wrap: anywhere; min-width: 8rem; max-width: 19rem"},
    {"name": "status", "label": "Статус", "field": "status", "align": "left"},
    {"name": "model", "label": "Модель", "field": "model", "align": "left"},
    {"name": "dataset", "label": "Датасет", "field": "dataset", "align": "left"},
    {"name": "tasks", "label": "Задачи: идут / очередь / готово", "field": "tasks", "align": "left"},
    {"name": "limit", "label": "Лимит", "field": "limit", "align": "right"},
    {"name": "started", "label": "Начат", "field": "started", "align": "left"},
]
RUN_STATUS_SLOT = """
<q-td :props="props" style="white-space: normal; overflow-wrap: anywhere; min-width: 10rem; max-width: 22rem">
  <div class="text-weight-medium" :class="props.row.status_color">{{ props.value }}</div>
  <div v-if="props.row.replicas" class="text-xs">{{ props.row.replicas }}</div>
  <div v-if="props.row.failed_workflows" class="text-negative text-xs">{{ props.row.failed_workflows }}</div>
  <div v-for="error in props.row.errors" :key="error" class="text-negative text-xs">{{ error }}</div>
</q-td>
"""
TASK_COLUMNS = [
    {"name": "task", "label": "Задача", "field": "task", "align": "left", "sortable": True},
    {"name": "phase", "label": "Argo", "field": "phase", "align": "left", "sortable": True},
    {"name": "result", "label": "S3: попытки · итог", "field": "result", "align": "left"},
    {"name": "duration", "label": "Длительность", "field": "duration", "align": "right"},
    {"name": "name", "label": "Workflow", "field": "name", "align": "left"},
]
PHASE_TEXT = {"Running": "идёт", "Pending": "ждёт", "": "ждёт", "Succeeded": "успех", "Failed": "упал", "Error": "ошибка"}
RESULT_TEXT = {"completed": "COMPLETED", "failed": "FAILED", "no_flag": "без флага", "no_attempt": "попыток нет"}
CUSTOM_DATASET = "__custom__"
REGISTRY_DATASET = "registry:"  # + имя записи реестра без группы harbor/dataset: путь, benchmark и маппинг подставляются
CUSTOM_LABEL = "свой путь под FLEET_DATASETS_ROOT…"
OWN_MODEL_PREFIX = "recipe:"
SESSION_PREFIX = "session:"


def _when(text: str | None) -> str:
    return datetime.fromisoformat(text).astimezone().strftime("%d.%m %H:%M") if text else "—"


def _matches_run_filters(run: BenchRun, hidden: list[str], owner: str | None, since: str, until: str) -> bool:
    status = "failed" if run.status == "done" and run.failed_attempts else run.status
    if status in hidden or (owner and run.owner_id != owner):
        return False
    if not (since or until):
        return True
    if not run.started_at:
        return False
    minute = datetime.fromisoformat(run.started_at).astimezone().strftime("%Y-%m-%dT%H:%M")
    return (not since or since <= minute) and (not until or minute <= until)


def _status_text(run: BenchRun) -> str:
    text = STATUS_TEXT.get(run.status, run.status)
    if not run.active and run.failed_attempts:
        outcome = "завершён с ошибками" if run.status == "done" else text
        return f"{outcome} · неудачных попыток: {run.failed_attempts}"
    if run.status == "attention" and "Модель недоступна:" in run.error:
        return "требует внимания · модель недоступна"
    if run.status == "queued":
        return "в очереди · ждёт свободной квоты"
    if run.status == "running" and run.stage == "check":
        return "ждёт готовности модели"
    if run.status == "stopping":
        return "останавливается · ждёт освобождения ресурсов"
    if run.status in {"running", "attention", "failed"} and run.stage:
        return f"{text} · {STAGE_TITLES.get(run.stage, run.stage)}"
    return text


def _status_color(run: BenchRun) -> str:
    return "text-negative" if run.status in {"attention", "failed"} or (not run.active and run.failed_attempts) else ""


def _error_text(message: str, secrets: Iterable[str]) -> str:
    # Старые записи могли сохранить команду вместе с телом ответа MLSC.
    command = re.search(r"kubectl exec -i [^\n]*?/api/v1/service/get_(?:configuration|last_failed_log)[^\n]*? → код (-?\d+)", message)
    if command:
        message = message[:command.start()] + f"ошибка запроса MLSC (код {command[1]})"
    elif "MLSC вернул не JSON:" in message:
        message = message.partition("MLSC вернул не JSON:")[0] + "MLSC вернул некорректный JSON"
    return scrub(message, secrets)


def _error_summary(message: str) -> str:
    line = " ".join(message.split())
    return line if len(line) <= 200 else line[:199] + "…"


def _run_mlsc(ctx: AppContext, run: BenchRun, secrets: Iterable[str]) -> tuple[str, str]:
    if run.version_id is None:
        return "", ""
    observer = ctx.observer
    if version := observer.model_version(run.version_id):
        error = f"MLSC · последняя ошибка: {scrub(version.last_deploy_error, secrets)}" if version.last_deploy_error else ""
        return f"Реплики готовы: {version.current}/{version.planned}", error
    if observer.current(observer.mlsc) is None:
        return "Данные MLSC недоступны", ""
    return "Версия не найдена в MLSC", ""


def _remaining_resources(ctx: AppContext, run: BenchRun) -> list[str]:
    if run.active:
        return []
    obs = ctx.observer
    argo = next((row for row in obs.current(obs.runs, since=run.finished_at) or [] if row.run_name == run.id), None)
    sub = next((row for row in obs.current(obs.submitter, since=run.finished_at) or [] if row.run_id == run.id), None)
    version = obs.active_model(run.id, run.version_id, since=run.finished_at) if run.own_model else None
    remaining = []
    if argo and argo.active:
        remaining.append(f"активные workflows: {argo.active}")
    if sub and sub.alive:
        remaining.append("сабмиттер работает")
    if version:
        remaining.append(f"версия MLSC {version.version_id} активна")
    return remaining


def _revision_title(revision: ReadyRevision) -> str:
    sha = revision.info.sha[:8]
    ref = f"{revision.ref}@{sha}" if revision.ref != revision.info.sha else sha
    merged = "влита в master" if revision.info.merged_into_main else f"не влита, отстаёт на {revision.info.behind_main}"
    return f"{ref} · {merged}"


def merged_groups(revision: ReadyRevision) -> dict[str, list[str]]:
    """Группы ревизии плюс overlay сервиса; члены overlay помечены суффиксом в подписи, но не в имени."""
    groups = {k: list(v) for k, v in revision.profile.groups.items()}
    for group, members in list_groups(OVERLAY_DIR).items():
        for m in members:
            if m not in groups.setdefault(group, []) and not (group == "" or m == "console_model"):
                groups[group].append(m)
    return groups


def overlay_members() -> dict[str, set[str]]:
    return {k: set(v) for k, v in list_groups(OVERLAY_DIR).items()}


def registry_datasets(revision: ReadyRevision) -> dict[str, Mapping]:
    """Бенчи из реестра datasets (ревизия + overlay), для каталога которых нет группы harbor/dataset."""
    roots = group_tasks_roots([revision.worktree / CONFIGS_REL, OVERLAY_DIR])
    return {REGISTRY_DATASET + m.name: m for m in registry_choices(list_mappings(revision.worktree, OVERLAY_DIR), roots)}


@dataclass
class Draft:
    """Черновик прогона между «Превью» и «Запустить»: каталог прогона уже есть, GPU ещё нет."""

    spec: BenchSpec | None = None
    resolved: dict[str, Any] | None = None
    resolve_error: str = ""
    preview: Preview | None = None
    checks: list[Check] = field(default_factory=list)
    manual: bool = False  # exp.yaml или файлы групп правили руками: форма их больше не перезаписывает
    run_id: str = ""
    drift: list[Drift] = field(default_factory=list)  # overlay-правки, чей оригинал изменился в этой ревизии


# --- страница --------------------------------------------------------------


async def benches_page(ctx: AppContext, run_id: str | None = None) -> None:
    build_header(ctx)
    ui.add_css("""
        .body--dark :is(.bench-runs, .bench-run-card, .bench-run-banner) { --q-negative: #ff929b; }
        @media (max-width: 599px) {
            .bench-runs table { table-layout: fixed; }
            .bench-runs th:first-child { width: 42%; }
            .bench-runs td { min-width: 0 !important; }
            .bench-runs th:nth-child(n+3), .bench-runs td:nth-child(n+3) { display: none; }
        }
    """)
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            ui.label("Прогоны").classes("text-xl")
            hint("Бенч — прогон задач датасета агентом на модели: от материализации до замера в Console.")
            ui.space()
            ui.button("Запустить бенч", on_click=lambda: _new_bench_dialog(ctx, render)).props("unelevated no-caps")
        banner = ui.label().classes("w-full text-negative text-sm whitespace-pre-wrap break-words bench-run-banner")
        with ui.expansion("Фильтры", icon="filter_list").classes("w-full").props("dense") as filters:
            with ui.row().classes("w-full items-end gap-3"):
                hidden = ui.select(
                    HIDDEN_STATUS_OPTIONS, multiple=True, clearable=True, label="Скрыть статусы",
                ).props("use-chips").classes("w-full sm:w-64")
                owner = ui.select({}, label="Владелец", clearable=True).classes("w-full sm:w-44")
                since = ui.input("Начат с").props("type=datetime-local stack-label").classes("w-full sm:w-52")
                until = ui.input("Начат по").props("type=datetime-local stack-label").classes("w-full sm:w-52")
                reset = ui.button("Сбросить", on_click=lambda: reset_filters()).props("flat no-caps")
        table = ui.table(columns=COLUMNS, rows=[], row_key="id").classes("w-full bench-runs").props("dense flat").mark("bench-runs")
        table.add_slot("body-cell-status", RUN_STATUS_SLOT)
        table.on("rowClick", lambda e: _run_dialog(ctx, e.args[1]["id"], render))
        empty = ui.label("Прогонов ещё нет. «Запустить бенч» собирает exp.yaml из пресета, показывает превью и preflight, затем ведёт стадии fleet.").classes(
            "text-sm opacity-70"
        )
        no_matches = ui.label("По этим фильтрам прогонов нет.").classes("text-sm opacity-70")
        table_hint = ui.label("клик по строке — карточка прогона: что и где запущено, стадии с командами, логи, остановка и повтор").classes("text-xs opacity-60")
        local_panel(ctx, lambda item: _new_bench_dialog(ctx, render, local=item))

    def render() -> None:
        access = ctx.access
        accessible = [r for r in ctx.benches.runs() if access.allows(r)]
        owners = {
            key: "Локальный владелец" if key == LOCAL_OWNER else key
            for key in sorted({r.owner_id for r in accessible})
        }
        if owner.options != owners:
            owner.set_options(owners, value=owner.value if owner.value in owners else None)
        runs = [r for r in accessible if _matches_run_filters(r, hidden.value or [], owner.value, since.value, until.value)]
        filtering = bool(hidden.value or owner.value or since.value or until.value)
        filters.set_text(f"Фильтры · найдено {len(runs)}" if filtering else "Фильтры")
        filters.set_visibility(bool(accessible))
        reset.set_visibility(filtering)
        remaining = {r.id: _remaining_resources(ctx, r) for r in runs}
        runs.sort(key=lambda run: not remaining[run.id])
        secrets = list(ctx.benches.secrets().values())
        summaries = {r.id: _error_summary(_error_text(r.error, secrets)) for r in runs}
        rows = []
        for r in runs[:50]:
            c = r.counters
            tasks = f"{c.get('running', 0)} / {c.get('queue') if c.get('queue') is not None else '—'} / {c.get('completed') if c.get('completed') is not None else '—'}"
            replicas, mlsc_error = _run_mlsc(ctx, r, secrets)
            rows.append(
                {
                    "id": r.id,
                    "model": r.model or r.recipe,
                    "dataset": dataset_of(r) if isinstance(dataset_of(r), str) else dataset_of(r).label,
                    "status": _status_text(r) + (" · активны ресурсы" if remaining[r.id] else ""),
                    "status_color": _status_color(r),
                    "replicas": replicas,
                    "failed_workflows": f"Сбои workflows: {c['failed']}" if c.get("failed") else "",
                    "errors": [err for err in (_error_summary(mlsc_error), summaries[r.id]) if err],
                    "tasks": tasks if r.status != "running" or r.stage == "execute" or c else "—",
                    "limit": r.agent_limit if r.agent_limit is not None else "—",
                    "started": _when(r.started_at),
                }
            )
        table.update_rows(rows)
        table.set_visibility(bool(rows))
        empty.set_visibility(not accessible)
        no_matches.set_visibility(bool(accessible) and not rows)
        reversed_range = since.value and until.value and since.value > until.value
        no_matches.set_text("Дата «Начат с» позже «Начат по»." if reversed_range else "По этим фильтрам прогонов нет.")
        table_hint.set_visibility(bool(rows))
        attention = [r for r in runs if r.status == "attention"]
        lines = [
            f"⚠ {r.id}: {'проверьте ресурсы модели' if r.holds_gpu else 'задачи могут идти'} — {summaries[r.id]}"
            + (f" · автогашение через {ctx.settings.bench.attention_minutes} мин без ответа" if r.holds_gpu and ctx.settings.bench.attention_minutes else "")
            for r in attention
        ]
        lines.extend(f"⚠ {r.id}: {summaries[r.id]}" for r in runs if r.status == "stopping" and r.error)
        banner.set_text(scrub("\n".join(lines), secrets))
        banner.set_visibility(bool(lines))

    def reset_filters() -> None:
        hidden.set_value([])
        owner.set_value(None)
        since.set_value("")
        until.set_value("")

    for field in (hidden, owner, since, until):
        field.on_value_change(render)
    render()
    ui.timer(3.0, render)
    if run_id:
        _run_dialog(ctx, run_id, render)


def _facts_table(rows: list[dict[str, str]]) -> ui.table:
    columns = [
        {"name": "k", "field": "k", "align": "left", "style": "width: clamp(7rem, 30%, 11rem); overflow-wrap: anywhere"},
        {"name": "v", "field": "v", "align": "left", "style": "overflow-wrap: anywhere"},
    ]
    return ui.table(columns=columns, rows=rows).props("dense flat hide-header wrap-cells table-style='table-layout: fixed'").classes("w-full")


# --- форма запуска -------------------------------------------------------------


def _new_bench_dialog(
    ctx: AppContext, after, local: LocalDataset | None = None, *,
    revision: ReadyRevision | None = None, initial: Preset | None = None, source: BenchRun | None = None,
) -> None:  # noqa: C901 — форма целиком, чтобы состояние жило в замыкании
    if local is not None:
        local = ctx.user_dataset(local.id)
    admin = ctx.access.admin
    revision = revision or ctx.revision
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label("Новый прогон из истории" if source else "Запустить бенч").classes("text-lg")
        if source:
            ui.label(f"Из прогона {source.id}. Будет создан новый прогон.").classes("text-xs opacity-70 break-all")
        if revision is None or not revision.profile.compatible:
            ui.label("Сначала подготовьте совместимую ревизию fleet на главной странице: прогон идёт её шагами.").classes("text-sm")
            ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
            dialog.open()
            return
        ui.label(f"ревизия {_revision_title(revision)}").classes("text-sm opacity-70")
        groups = merged_groups(revision)
        overlay = overlay_members()
        presets, preset_errors = load_presets(PRESETS_DIR)
        personal, personal_errors = load_presets(personal_dir(ctx.paths.home, ctx.access.id))
        personal_by_key = {"personal:" + p.name: p for p in personal}
        by_key = {p.name: p for p in presets} | personal_by_key
        draft = Draft()
        model_config_changed = False
        applied_files: dict[str, str] = {}
        setup: Preset | None = None

        def label_of(group: str, member: str) -> str:
            return f"{member} (overlay)" if member in overlay.get(group, set()) else member

        local_items = {item.id: item for item in ctx.user_datasets()}
        local_ids = {item.id for item in ctx.local_datasets.all()}

        def visible_mapping(mapping: Mapping) -> bool:
            private = mapping.name in local_ids or mapping.tasks_root.strip("/").startswith("local/")
            return admin or not private or mapping.name in local_items

        registry = {key: m for key, m in registry_datasets(revision).items() if visible_mapping(m)}

        def dataset_options(members: list[str]) -> dict[str, str]:
            """Группы, затем бенчи только из реестра (путь подставится), затем свой путь."""
            options = {d: label_of("harbor/dataset", d) for d in members}
            options |= {key: (f"{local_items[m.name].name} · из локальной папки" if m.name in local_items else
                             f"{m.name} · из реестра" + (" (overlay)" if m.origin == "overlay" else "")) for key, m in registry.items()}
            return options | ({CUSTOM_DATASET: CUSTOM_LABEL} if admin else {})

        # --- поля формы
        preset_options = {"": "— без пресета —"} | {
            key: f"{p.name} · личный" if key in personal_by_key else f"{p.name} · {p.description}" if p.description else p.name
            for key, p in by_key.items()
        }
        preset_select = ui.select(preset_options, value="", label="Пресет").classes("w-full")
        preset_note = ui.label().classes("text-xs whitespace-pre-wrap")
        if preset_errors or personal_errors:
            ui.label("битые пресеты: " + "; ".join(preset_errors + personal_errors)).classes("text-xs text-negative")

        model_options: dict[str, str] = {}
        for recipe in groups.get("mlsc/model", []):
            summary_ = recipe_summary(revision.worktree, recipe) if recipe not in overlay.get("mlsc/model", set()) else {}
            gpu = summary_.get("gpu_per_instance")
            model_options[OWN_MODEL_PREFIX + recipe] = f"поднять: {label_of('mlsc/model', recipe)}" + (f" · {gpu} GPU/инстанс" if gpu else "")
        for s in ctx.sessions.sessions(active_only=True):
            if s.status == "ready" and s.owner_id == ctx.access.id:
                model_options[SESSION_PREFIX + s.id] = f"сессия: {s.model} · {s.replicas} реплик · версия {s.version_id}"
        with ui.row().classes("w-full gap-3 flex-wrap items-end"):
            model_select = ui.select(model_options, value=next(iter(model_options), None), label="Модель", with_input=True).classes("flex-1 min-w-[18rem]")
            model_name = ui.input("Имя модели (уникальный суффикс добавится автоматически)").classes("flex-1 min-w-[14rem]")
            queue, priority = job_options_select()
        with ui.row().classes("w-full gap-3 flex-wrap items-end"):
            agents = groups.get("harbor/agent", [])
            agent_select = ui.select({a: label_of("harbor/agent", a) for a in agents}, value=agents[0] if agents else None, label="Агент · harbor/agent", with_input=True).classes(
                "flex-1 min-w-[16rem]"
            )
            datasets = groups.get("harbor/dataset", [])
            dataset_select = ui.select(
                dataset_options(datasets), value=datasets[0] if datasets else next(iter(registry), CUSTOM_DATASET), label="Датасет · группа или реестр", with_input=True
            ).classes("flex-1 min-w-[16rem]")
        with ui.row().classes("w-full gap-3 flex-wrap items-end") as custom_row:
            custom_path = ui.input("путь датасета относительно FLEET_DATASETS_ROOT").classes("flex-1 min-w-[14rem]")
            custom_benchmark = ui.input("benchmark").classes("w-60")  # имена из реестра длинные: cowork-bench-ua-external-260902
            mapping_names = [m.name for m in list_mappings(revision.worktree, OVERLAY_DIR) if visible_mapping(m)]
            custom_mapping = ui.select({"": "маппинг по tasks_root"} | {m: m for m in mapping_names}, value="", label="реестр datasets").classes("w-56")
        local_note = ui.label().classes("text-sm break-all").mark("local-origin")
        with ui.row().classes("gap-3 flex-wrap items-end"):
            jobs = ui.number("Реплики", value=1, min=1, precision=0).classes("w-20")
            apr = ui.number("Агентов на реплику", value=1, min=1, precision=0).classes("w-36")
            task_limit = ui.number("Лимит задач (пусто — все)", value=None, min=1, precision=0).classes("w-48")
            repeats = ui.number("Попыток на задачу", value=1, min=1, step=1).classes("w-36")
            profile = ui.select(groups.get("execution", ["production"]), value="production" if "production" in groups.get("execution", []) else None, label="Профиль").classes("w-32")
            inference = ui.select({"litellm": "litellm", "nginx": "nginx"}, value="litellm", label="Маршрут").classes("w-24")
        gpu_label = ui.label().classes("text-sm")
        with ui.expansion("Дополнительно").classes("w-full") as advanced:
            extra = ui.textarea("overrides fleet, по одному на строку: ключ=значение").classes("w-full").props("autogrow")
            harbor_job_text = ui.textarea("harbor_job поверх (YAML): таймауты, retry…").classes("w-full").props("autogrow")
            config_button = ui.button("Полный конфиг: файлы, комментарии, правка", on_click=lambda: _config_dialog(ctx, draft, rebuild, refresh_groups)).props(
                "flat no-caps"
            )
            config_button.disable()
        advanced.set_visibility(admin)
        carry_files = ui.checkbox("Сохранить ручные правки YAML").set_visibility(False)
        manual_note = ui.label(
            "Сохранённый exp.yaml задаёт поля формы. Его можно изменить в полном конфиге или пересобрать из полей, сняв галочку."
        ).classes("text-xs opacity-70").set_visibility(False)
        preview_box = ui.column().classes("w-full gap-1")
        checks_box = ui.column().classes("w-full gap-1")
        accept = ui.checkbox("Принимаю предупреждения").mark("accept-warnings")
        accept.set_visibility(False)
        refresh_note = ui.label("Параметры запуска изменились. Обновите превью и preflight.").classes("text-sm text-warning")
        refresh_note.set_visibility(False)
        with ui.row().classes("gap-2 items-center"):
            preview_button = ui.button("Превью и preflight", on_click=lambda: rebuild()).props("unelevated no-caps")
            start_button = ui.button("Запустить", on_click=lambda: ask()).props("unelevated no-caps color=positive").mark("start-bench")
            start_button.disable()
            ui.button("Отмена", on_click=dialog.close).props("flat no-caps")
            busy = ui.spinner(size="sm")
            busy.set_visibility(False)

        # --- поведение
        def refresh_groups() -> None:
            """После «Сохранить в overlay»: новый член группы появляется в списках формы."""
            fresh = merged_groups(revision)
            agent_select.set_options({a: label_of("harbor/agent", a) for a in fresh.get("harbor/agent", [])}, value=agent_select.value)
            registry.clear()
            registry.update(registry_datasets(revision))
            dataset_select.set_options(dataset_options(fresh.get("harbor/dataset", [])), value=dataset_select.value)
            overlay.clear()
            overlay.update(overlay_members())

        def is_path_dataset() -> bool:
            return dataset_select.value == CUSTOM_DATASET or str(dataset_select.value or "") in registry

        def fill_from_registry(value: Any) -> None:
            """Бенч из реестра: путь, benchmark и маппинг — из записи; поля остаются видимыми и правятся."""
            m = registry.get(str(value or ""))
            if m is not None:
                custom_path.set_value(m.tasks_root)
                custom_benchmark.set_value(bench_slug(m.benchmark))
                custom_mapping.set_value(m.name if m.name in custom_mapping.options else "")
            item = local_items.get(m.name) if m else None
            local_note.set_text(f"Из локальной папки: {item.source} · " + ("задачи загружены в S3" if item.uploaded_at else "ещё не отправлены в S3") if item else "")

        def is_session() -> bool:
            return str(model_select.value or "").startswith(SESSION_PREFIX)

        def refresh_gpu() -> None:
            selected_mapping = registry.get(str(dataset_select.value or ""))
            custom_row.set_visibility(admin and is_path_dataset() and not (selected_mapping and selected_mapping.name in local_items))
            model_name.set_visibility(not is_session())
            priority.set_visibility(bool(model_select.value) and not is_session())
            queue.set_visibility(bool(model_select.value) and not is_session())
            n = int(jobs.value or 1)
            if model_select.value not in model_options:
                return
            if is_session():
                s = ctx.access.session(model_select.value[len(SESSION_PREFIX):])
                gpu_label.set_text(f"модель уже поднята: сессия {s.model}, реплик {s.replicas}; подъём и гашение пропускаются, таймер сессии стоит")
                jobs.set_value(s.replicas)
                jobs.disable()
                return
            jobs.enable()
            recipe = str(model_select.value or "")[len(OWN_MODEL_PREFIX):]
            gpu = recipe_summary(revision.worktree, recipe).get("gpu_per_instance") if recipe else None
            gpu_label.set_text(f"GPU: {n} × {gpu} = {n * gpu} · лимит агентов {n} × {int(apr.value or 1)} = {n * int(apr.value or 1)}" if gpu else f"лимит агентов {n * int(apr.value or 1)}")

        def fill_setup(preset: Preset, *, session_id: str | None = None, exact: bool = False) -> None:
            nonlocal setup
            setup = preset
            key = SESSION_PREFIX + session_id if session_id else OWN_MODEL_PREFIX + preset.model
            problems = compatibility(preset, groups, revision.profile.steps, [])
            if session_id and key not in model_options:
                problems.append("исходная сессия модели не готова или недоступна; выберите модель")
            preset_note.set_text("несовместим с ревизией: " + "; ".join(problems) if problems else f"Параметры из «{preset.name}» · fleet {preset.fleet.ref[:12]}")
            preset_note.classes(replace="text-xs whitespace-pre-wrap " + ("text-negative" if problems else "opacity-70"))
            if key in model_options or exact:
                model_select.set_value(key if key in model_options else None)
            model_name.set_value(preset.model_name)
            if preset.agent in agent_select.options or exact:
                agent_select.set_value(preset.agent if preset.agent in agent_select.options else None)
            if isinstance(preset.dataset, str):
                if preset.dataset in dataset_select.options or exact:
                    dataset_select.set_value(preset.dataset if preset.dataset in dataset_select.options else None)
            else:
                ref = preset.dataset
                key = next((k for k, m in registry.items() if m.name == ref.mapping or m.tasks_root.strip("/") == ref.path.strip("/")), CUSTOM_DATASET)
                if key in dataset_select.options or exact:
                    dataset_select.set_value(key if key in dataset_select.options else None)
                fill_from_registry(dataset_select.value)
                custom_path.set_value(ref.path)
                custom_benchmark.set_value(ref.benchmark)
                custom_mapping.set_value(ref.mapping if ref.mapping in custom_mapping.options else "")
            jobs.set_value(preset.resources.jobs)
            apr.set_value(preset.resources.agents_per_replica)
            task_limit.set_value(preset.execution.task_limit)
            if preset.execution.profile in profile.options or exact:
                profile.set_value(preset.execution.profile if preset.execution.profile in profile.options else None)
            inference.set_value(preset.inference)
            saved_repeats, preset_overrides = preset.repeat_option()
            repeats.set_value(saved_repeats)
            saved_priority, saved_queue, overrides = split_job_options(preset_overrides)
            queue.set_value(saved_queue)
            set_job_priority_options(priority, saved_queue, saved_priority)
            extra.set_value("\n".join(overrides))
            harbor_job_text.set_value(yaml.safe_dump(preset.harbor_job, allow_unicode=True, sort_keys=False) if preset.harbor_job else "")
            carry_files.set_visibility(admin and bool(preset.config_files))
            carry_files.set_value(admin and bool(preset.config_files) and not (session_id and model_select.value is None))
            sync_manual_fields()

        async def apply_preset(name: str | None) -> None:
            nonlocal setup
            mark_model_config_changed()
            preset = by_key.get(name or "")
            if preset is None:
                setup = None
                carry_files.set_value(False)
                carry_files.set_visibility(False)
                preset_note.set_text("")
                sync_manual_fields()
                mark_model_config_changed()
                return
            try:
                if name in personal_by_key:
                    _check_saved_setup(ctx, preset, presets)
                    if preset.fleet.ref != revision.info.sha:
                        new_revision = await ctx.manager.prepare(preset.fleet.ref)
                        if preset_select.value == name:
                            _new_bench_dialog(ctx, after, revision=new_revision, initial=preset)
                            dialog.close()
                        return
                fill_setup(preset, exact=name in personal_by_key)
            except (AccessDenied, BenchError, PrepareError) as exc:
                ui.notify(str(exc), type="negative", multi_line=True)

        def sync_manual_fields() -> None:
            frozen = bool(carry_files.value and setup and "exp.yaml" in setup.config_files)
            for element in (model_select, agent_select, dataset_select, custom_path, custom_benchmark, custom_mapping,
                            apr, task_limit, profile, inference, harbor_job_text):
                element.set_enabled(not frozen)
            repeats.set_enabled(not (frozen or draft.manual))
            manual_note.set_visibility(frozen)
            refresh_gpu()
            if frozen:
                jobs.disable()

        def dataset_value() -> str | DatasetRef:
            if is_path_dataset():
                if not admin:
                    mapping = registry.get(str(dataset_select.value))
                    if mapping is None:
                        raise AccessDenied()
                    return DatasetRef(path=mapping.tasks_root, benchmark=bench_slug(mapping.benchmark), mapping=mapping.name)
                return DatasetRef(path=(custom_path.value or "").strip(), benchmark=(custom_benchmark.value or "").strip(), mapping=(custom_mapping.value or None))
            return str(dataset_select.value)

        def build_spec() -> BenchSpec:
            selected_personal = personal_by_key.get(preset_select.value)
            if selected_personal is not None and setup != selected_personal:
                raise BenchError("Личный пресет ещё не применён; проверьте сообщение о подготовке ревизии")
            if model_select.value not in model_options or agent_select.value not in agent_select.options or dataset_select.value not in dataset_select.options:
                raise BenchError("выберите модель, агента и датасет из списка")
            if not admin and (profile.value not in profile.options or inference.value not in {"litellm", "nginx"}
                              or not re.fullmatch(r"[A-Za-z0-9_.-]*", model_name.value or "")):
                raise BenchError("проверьте профиль, маршрут и имя модели")
            dataset = dataset_value()
            if isinstance(dataset, DatasetRef) and not (dataset.path and dataset.benchmark):
                raise BenchError("для своего датасета нужны путь и benchmark")
            if not model_select.value or not agent_select.value or profile.value not in profile.options:
                raise BenchError("выберите модель, агента и профиль")
            attempts = repeats.value
            if attempts is None or attempts < 1 or not float(attempts).is_integer():
                raise BenchError("Попыток на задачу: задайте целое число от 1")
            if admin:
                try:
                    harbor_job = yaml.safe_load(harbor_job_text.value or "") or {}
                except yaml.YAMLError as exc:
                    raise BenchError(f"harbor_job: не YAML — {exc}") from exc
            else:
                harbor_job = setup.harbor_job if setup else {}
            if not isinstance(harbor_job, dict):
                raise BenchError("harbor_job должен быть словарём")
            session = None
            recipe = str(model_select.value)[len(OWN_MODEL_PREFIX):]
            if is_session():
                session_id = str(model_select.value)[len(SESSION_PREFIX):]
                ctx.access.session(session_id)
                session = ctx.benches.session_link(session_id)
                recipe = session.recipe
            overrides = ([line.strip() for line in (extra.value or "").splitlines() if line.strip()] if admin
                         else split_job_options(setup.repeat_option()[1])[2] if setup else [])
            if not session and priority.value in JOB_PRIORITIES:
                overrides.append(f"{JOB_PRIORITY_OVERRIDE}={priority.value}")
            if not session and queue.value in JOB_QUEUES:
                overrides.append(f"++{JOB_QUEUE_KEY}={queue.value}")
            preset_name = setup.name if setup and not source else ""
            base_preset = (preset_select.value if preset_select.value and preset_select.value not in personal_by_key
                           else setup.base_preset) if setup else ""
            if not draft.run_id:
                base = preset_name or f"{session.model if session else recipe}-{dataset if isinstance(dataset, str) else dataset.benchmark}"
                run_id = make_bench_id(base, datetime.now(timezone.utc), uuid4().hex[:10])
                ctx.paths.runs.joinpath(run_id).mkdir(exist_ok=False)
                ctx.store.reserve_draft(run_id, ctx.access.id, revision.info.sha)
                draft.run_id = run_id
            item = next((d for d in local_items.values() if isinstance(dataset, DatasetRef) and ctx.settings.datasets_root_path() is not None and
                         (ctx.settings.datasets_root_path() / dataset.path).resolve() == Path(d.path).resolve()), None)
            if item:
                item = ctx.user_dataset(item.id)
            return BenchSpec(
                run_id=draft.run_id, worktree=revision.worktree, run_dir=ctx.paths.runs / draft.run_id, overlay=OVERLAY_DIR, recipe=recipe,
                agent=str(agent_select.value), dataset=dataset, jobs=int(jobs.value or 1), agents_per_replica=int(apr.value or 1),
                task_limit=int(task_limit.value) if task_limit.value else None, profile=str(profile.value or "production"), inference=str(inference.value),
                repeats=int(attempts),
                model_name=(model_name.value or "").strip() if not session else "", harbor_job=harbor_job,
                private_s3_root=private_root(ctx.settings),
                extra=tuple(overrides), pod=draft.spec.pod if draft.spec else "",
                session=session, preset=base_preset, sha=revision.info.sha,
                dataset_source={"id": item.id, "source": item.source, "imported_at": item.imported_at, "s3_uri": item.s3_uri,
                                "uploaded_at": item.uploaded_at, "digest": item.digest} if item else {},
            )

        async def rebuild() -> None:
            """Черновик: exp.yaml → реестр → --print-config → превью и preflight."""
            nonlocal model_config_changed
            busy.set_visibility(True)
            preview_button.disable()
            start_button.disable()
            try:
                spec = build_spec()
                ctx.access.draft(spec.run_id)
                model_config_changed = False
                if not spec.pod:
                    spec = BenchSpec(**{**spec.__dict__, "pod": await ctx.benches._pod()})
                ctx.access.draft(spec.run_id)
                files = setup.config_files if admin and setup and carry_files.value else {}
                for rel, old in applied_files.items():
                    path = spec.run_dir / config_path(rel)
                    if rel not in files and path.is_file() and path.read_text(encoding="utf-8") == old:
                        path.unlink()
                for rel, content in files.items():
                    if applied_files.get(rel) == content:
                        continue
                    path = spec.run_dir / config_path(rel)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                applied_files.clear()
                applied_files.update(files)
                spec.write(force=not draft.manual and "exp.yaml" not in files)
                draft.spec = spec
                mappings, registry_errors = build_registry(spec.worktree, spec.overlay, spec.run_dir, include=visible_mapping)
                draft.resolved, draft.resolve_error = await ctx.benches.print_config(spec)
                session_status = ctx.sessions.get(spec.session.id).status if spec.session else None
                draft.preview = from_resolved(
                    draft.resolved or {}, revision=_revision_title(revision), context=ctx.kube_status.context or "", run_dir=spec.run_dir,
                    session=spec.session.id if spec.session else "",
                )
                foreign = {v.model for v in ctx.observer.mlsc.data or [] if not any(o.owns_version(v.version_id, v.application) for o in ctx.store.own_runs().values())}
                draft.drift = await _overlay_drift(ctx, spec, revision.info.sha)
                draft.checks = run_checks(
                    secrets=ctx.benches.secrets(), kube=ctx.kube_status, profile=revision.profile, own_model=spec.session is None, resolved=draft.resolved,
                    resolve_error=draft.resolve_error, datasets_root=ctx.settings.datasets_root_path(), mappings=mappings, registry_errors=registry_errors,
                    foreign_models=foreign, model_name=spec.effective_model or draft.preview.model, agent=spec.agent, recipe=spec.recipe,
                    dataset=spec.dataset_label, issues=load_issues(), session_status=session_status, overlay_drift=[d.summary for d in draft.drift],
                )
                if draft.resolved is not None:
                    problem = await model_route_problem(
                        spec, draft.resolved, draft.preview, ctx.benches.runner, ctx.settings, session_ready=session_status == "ready",
                    )
                    draft.checks.append(Check("model_route", "Маршрут модели", "fail" if problem else "ok",
                                              problem or f"{draft.preview.inference} · {draft.preview.model}"))
                if draft.resolved is not None:
                    draft.checks.append(await ctx.benches.check_harbor_tasks(spec, draft.resolved))
                if draft.resolved:
                    draft.checks.append(await task_resources(ctx.benches.runner, spec.worktree, draft.resolved, ctx.kube_status))
                if draft.resolved is not None:
                    draft.checks.extend(await task_issue_checks(ctx.benches.runner, spec, draft.resolved))
                if not supports_private_measure(spec.worktree):
                    draft.checks.append(Check("private_measure", "Приватный замер", "fail", "Ревизия не поддерживает read_only"))
                if spec.dataset_source:
                    item = ctx.user_dataset(spec.dataset_source["id"])
                    problem = await ctx.local_datasets.remote_ready(item.id)
                    draft.checks.append(Check("local_upload", "Локальные задачи в S3", "fail" if problem else "ok", problem or item.s3_uri))
            except (AccessDenied, BenchError, OSError, ValueError) as exc:
                ui.notify(str(exc), type="negative", multi_line=True)
                return
            finally:
                busy.set_visibility(False)
                preview_button.enable()
            config_button.enable()
            if draft.manual:
                repeats.disable()
            render_preview()

        def render_preview() -> None:
            preview_box.clear()
            checks_box.clear()
            if draft.preview is None:
                return
            with preview_box:
                ui.label("Что и где будет запущено").classes("text-base mt-2")
                if draft.resolve_error:
                    ui.label(draft.resolve_error).classes("text-negative text-sm whitespace-pre-wrap font-mono")
                else:
                    rows = [{"k": k, "v": v} for k, v in draft.preview.rows()]
                    _facts_table(rows)
                edits = manual_edits(draft.spec.run_dir, draft.spec.worktree, draft.spec.overlay, draft.spec.exp.read_text(encoding="utf-8")) if draft.spec else []
                if draft.manual or edits:
                    ui.label("ручные правки: " + (", ".join(name for name, _ in edits) or "exp.yaml")).classes("text-xs text-warning")
            fails, warns = summary(draft.checks)
            with checks_box:
                ui.label(f"Preflight: ✗ {fails} · ⚠ {warns}").classes("text-base mt-2")
                for c in draft.checks:
                    icon, color = LEVEL_ICON[c.level]
                    long_detail = c.key == "task_resources" or c.key.startswith("issue:")
                    wrap = "sm:flex-nowrap" if long_detail else "no-wrap"
                    with ui.row().classes(f"items-start gap-2 {wrap}"):
                        ui.icon(icon).classes(f"{color} text-base mt-0.5")
                        ui.label(c.title).classes("text-sm whitespace-nowrap")
                        with ui.column().classes("gap-0 min-w-0"):
                            word_wrap = "break-words" if long_detail else "break-all"
                            ui.label(c.detail).classes(f"text-xs opacity-70 {word_wrap}")
                            if admin and c.action and c.action.get("kind") == "fetch_dataset":
                                ui.button("Скачать из S3", on_click=lambda a=c.action: _fetch_dataset_dialog(ctx, draft, rebuild, a)).props("flat dense no-caps").classes(
                                    "self-start"
                                )
            accept.set_visibility(warns > 0)
            accept.set_value(False)
            refresh_start()

        def refresh_start() -> None:
            fails, warns = summary(draft.checks)
            refresh_note.set_visibility(draft.preview is not None and model_config_changed)
            ok = (preview_button.enabled and not model_config_changed and draft.spec is not None and draft.resolved is not None
                  and fails == 0 and (warns == 0 or bool(accept.value)))
            start_button.enable() if ok else start_button.disable()

        def mark_model_config_changed() -> None:
            nonlocal model_config_changed
            model_config_changed = True
            refresh_start()

        def ask() -> None:
            spec, preview = draft.spec, draft.preview
            if not start_button.enabled or spec is None or preview is None:
                return
            gpu = preview.gpu_total
            stages = ("preflight → материализация → " + ("" if spec.session else "create_job → start_llm → check → ") + "upload → launch → исполнение"
                      + (f" → requeue ×{ctx.settings.bench.requeue_rounds}" if ctx.settings.bench.requeue_rounds else "") + ("" if spec.session else " → stop_llm") + " → замер")
            lines = [
                f"{preview.model} × {spec.dataset_label} × {spec.agent}; реплик {preview.replicas}, агентов на реплику {spec.agents_per_replica}",
                f"GPU: {gpu if gpu is not None else '?'}" + (" — модель уже поднята (сессия)" if spec.session else " — сервис поднимет и погасит модель сам"),
                f"задач: {preview.tasks_total if preview.tasks_total is not None else 'узнаем после материализации'}" + (f", лимит {spec.task_limit}" if spec.task_limit else ""),
                f"Попыток на задачу: {preview.repeats}",
                "стадии: " + stages,
                f"результаты: {preview.s3}",
            ]
            if preview.job_priority and not spec.session:
                lines.insert(2, f"приоритет джобы {preview.job_priority}")
            if preview.queue and not spec.session:
                lines.insert(2, f"очередь MLSC {preview.queue}")
            if ctx.guard.supported:
                lines.append("Прогон займёт часы: не закрывайте крышку Mac и не отключайте питание — caffeinate этого не остановит; без присмотра надёжнее сервер.")
                if ctx.guard.battery and not ctx.guard.battery.on_ac:
                    lines.append(f"Mac на батарее ({ctx.guard.battery.percent}%): подключите зарядку.")

            async def start() -> None:
                if spec.dataset_source:
                    ctx.user_dataset(spec.dataset_source["id"])
                    problem = await asyncio.to_thread(ctx.local_datasets.ready, spec.dataset_source["id"])
                    if problem:
                        ui.notify(problem, type="negative")
                        return
                run = await ctx.benches.start(spec, ref=revision.ref, preview=preview, access=ctx.access)
                dialog.close()
                ui.notify(f"Заявка принята: {run.id} · {_status_text(run)}")

            confirm_dialog("Запустить прогон?", lines, start, after, button="Да, запустить")

        def discard_draft() -> None:
            """Форма закрыта без запуска: каталог черновика (exp.yaml, resolved.yaml) больше не нужен."""
            if draft.run_id and ctx.store.get_bench(draft.run_id) is None and ctx.store.draft_owner(draft.run_id) == ctx.access.id:
                shutil.rmtree(ctx.paths.runs / draft.run_id, ignore_errors=True)
                ctx.store.delete_draft(draft.run_id)

        preset_select.on_value_change(lambda e: apply_preset(e.value))
        dataset_select.on_value_change(lambda e: fill_from_registry(e.value))
        for element in (model_select, dataset_select, jobs, apr):
            element.on_value_change(lambda _: refresh_gpu())
        for element in (model_select, model_name, priority, queue, extra, agent_select, dataset_select,
                        custom_path, custom_benchmark, custom_mapping, jobs, apr, task_limit, repeats, profile, inference, harbor_job_text):
            element.on_value_change(lambda _: mark_model_config_changed())
        carry_files.on_value_change(lambda _: (sync_manual_fields(), mark_model_config_changed()))
        accept.on_value_change(lambda _: refresh_start())
        dialog.on("hide", lambda _: discard_draft())
        fill_from_registry(dataset_select.value)
        if local:
            dataset_select.set_value(REGISTRY_DATASET + local.id)
            fill_from_registry(dataset_select.value)
        if initial is not None:
            if not source:
                preset_select.value = next((key for key, p in personal_by_key.items() if p.name == initial.name), "")
            fill_setup(initial, session_id=source.session_id if source else None, exact=True)
            if source and model_select.value and agent_select.value and dataset_select.value and profile.value:
                ui.timer(0, rebuild, once=True)
        refresh_gpu()
    dialog.open()


def _check_saved_setup(ctx: AppContext, setup: Preset, shipped: list[Preset]) -> None:
    if ctx.access.admin:
        return
    base = next((p for p in shipped if p.name == setup.base_preset), None)
    if (setup.config_files or setup.harbor_job != (base.harbor_job if base else {})
            or split_job_options(setup.repeat_option()[1])[2] != (split_job_options(base.repeat_option()[1])[2] if base else [])):
        raise BenchError("Этот запуск содержит расширенные настройки вне штатного пресета; для повтора нужны права администратора.")


def _saved_run_setup(ctx: AppContext, run_id: str) -> tuple[BenchRun, Preset]:
    run = ctx.access.run(run_id)
    directory = Path(run.dir)
    spec = BenchSpec.from_json((directory / "spec.json").read_text(encoding="utf-8"))
    if spec.dataset_source:
        ctx.user_dataset(spec.dataset_source["id"])
    setup = preset_from_run(spec, directory, name=run.id, sha=run.sha)
    _check_saved_setup(ctx, setup, load_presets(PRESETS_DIR)[0])
    return run, setup


async def _repeat_run(ctx: AppContext, run_id: str, after, close) -> None:
    try:
        run, setup = _saved_run_setup(ctx, run_id)
        revision = await ctx.manager.prepare(run.sha)
    except (AccessDenied, BenchError, PrepareError, OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        ui.notify(f"Не удалось повторить прогон: {exc}", type="negative", multi_line=True)
        return
    _new_bench_dialog(ctx, after, revision=revision, initial=setup, source=run)
    close()


def _save_run_preset_dialog(ctx: AppContext, run_id: str) -> None:
    try:
        run, setup = _saved_run_setup(ctx, run_id)
    except (AccessDenied, BenchError, OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        ui.notify(f"Не удалось прочитать настройки прогона: {exc}", type="negative", multi_line=True)
        return
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
        ui.label("Личный пресет из прогона").classes("text-lg")
        ui.label(f"Ревизия fleet закреплена: {run.sha[:12]}").classes("text-sm")
        if run.session_id:
            ui.label("Пресет выберет рецепт модели из прогона. Готовую сессию можно выбрать в форме.").classes("text-sm")
        name = ui.input("Имя пресета", value=run.id).classes("w-full")
        error = ui.label().classes("text-negative text-sm")

        def save() -> None:
            try:
                ctx.access.run(run_id)
                _check_saved_setup(ctx, setup, load_presets(PRESETS_DIR)[0])
                preset = setup.model_copy(update={"name": name.value.strip()})
                raw = preset.model_dump_json()
                if (scrub(raw, ctx.benches.secrets().values()) != raw
                        or any(scrub(content, ctx.benches.secrets().values()) != content
                               for content in (*setup.config_files.values(), *setup.overrides))):
                    raise BenchError("В настройках найден секрет. Вынесите его в окружение перед сохранением пресета.")
                save_personal(personal_dir(ctx.paths.home, ctx.access.id), preset)
            except FileExistsError:
                error.set_text("Личный пресет с таким именем уже существует.")
                return
            except (AccessDenied, BenchError, OSError, ValueError) as exc:
                error.set_text(str(exc))
                return
            dialog.close()
            ui.notify(f"Пресет «{preset.name}» сохранён. Его можно выбрать при запуске бенча.")

        with ui.row().classes("gap-2"):
            ui.button("Сохранить пресет", on_click=save).props("unelevated no-caps").mark("save-personal-preset")
            ui.button("Отмена", on_click=dialog.close).props("flat no-caps")
    dialog.open()


# --- полный конфиг ---------------------------------------------------------------


def _fetch_dataset_dialog(ctx: AppContext, draft: Draft, rebuild, action: dict[str, Any]) -> None:
    """Скачать задачи датасета из S3-зеркала fleet по реестру: весь датасет, задачи из конфига или первые N."""
    ctx.access.require_admin()
    ctx.access.draft(draft.run_id)
    spec, resolved = draft.spec, draft.resolved or {}
    root = ctx.settings.datasets_root_path()
    system = resolved.get("system") or {}
    s3_root = str(system.get("s3_datasets_root") or "")
    if not s3_root and system.get("bucket") and system.get("namespace"):  # старые ревизии без ключа: та же раскладка, что в system/cluster.yaml
        s3_root = f"{str(system['bucket']).rstrip('/')}/{system['namespace']}/harbor_datasets"
    mappings, _ = build_registry(spec.worktree, spec.overlay, spec.run_dir) if spec else ([], [])
    mapping = next((m for m in mappings if m.name == action.get("mapping")), None)
    names = [str(n) for n in action.get("task_names") or []]
    limit = (spec.task_limit if spec else None) or 3
    state: dict[str, Any] = {"plan": None, "job": None}
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-3xl"):
        ui.label(f"Скачать датасет из S3 · {action.get('mapping')}").classes("text-lg")
        if spec is None or mapping is None or root is None or not s3_root:
            ui.label("не хватает данных: нужен собранный конфиг с system.s3_datasets_root, корень датасетов в настройках и маппинг в реестре").classes("text-negative text-sm")
            ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
            dialog.open()
            return
        ui.label(f"реестр {mapping.name} ({mapping.origin}): S3 {mapping.task_s3_base} → локально {mapping.tasks_root}; в каждой задаче fleet ждёт task.toml с environment.docker_image").classes(
            "text-xs opacity-70"
        )
        options = {"first": f"первые {limit} задач по имени (как execution.task_limit)", "all": "весь датасет"}
        if names:
            options = {"names": "задачи из конфига: " + ", ".join(names)} | options
        mode = ui.radio(options, value=next(iter(options))).props("dense")
        plan_box = ui.column().classes("w-full gap-1")
        progress = ui.label().classes("text-sm font-mono whitespace-pre-wrap")
        with ui.row().classes("gap-2 items-center"):
            start_button = ui.button("Скачать", on_click=lambda: start()).props("unelevated no-caps").mark("fetch-dataset")
            again = ui.button("Превью и preflight", on_click=lambda: finish()).props("unelevated no-caps color=positive")
            again.set_visibility(False)
            ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")

        async def make_plan() -> None:
            tasks: tuple[str, ...] = ()
            try:
                if mode.value == "names":
                    tasks = tuple(names)
                elif mode.value == "first":
                    tasks = tuple((await ctx.datasets.task_dirs(f"{s3_root.rstrip('/')}/{mapping.task_s3_base.strip('/')}/"))[:limit])
            except Exception as exc:  # noqa: BLE001 — нет ключей или aws: показываем и даём скачать всё
                progress.set_text(f"список задач не прочитать: {exc}")
            plan = plan_fetch(mapping, root, s3_root, tasks)
            state["plan"] = plan
            plan_box.clear()
            with plan_box:
                ui.label(f"{plan.src} → {plan.dst}" + (f" · задач: {len(plan.tasks)}" if plan.tasks else " · все задачи")).classes("text-sm break-all")
                ui.code(plan.command, language="bash").classes("w-full")

        async def start() -> None:
            ctx.access.require_admin()
            ctx.access.draft(draft.run_id)
            if state["plan"] is None:
                await make_plan()
            try:
                job = ctx.datasets.fetch(state["plan"])
            except RuntimeError as exc:
                progress.set_text(str(exc))
                return
            state["job"] = job
            start_button.disable()
            mode.disable()
            timer = ui.timer(0.5, lambda: poll(timer))
            poll(timer)

        def poll(timer: ui.timer) -> None:
            job = state["job"]
            progress.set_text(f"{'идёт' if job.active else 'готово' if job.status == 'done' else 'не удалось'}: строк вывода {job.lines} · {job.last}\nлог: {job.log}")
            if not job.active:
                timer.deactivate()
                again.set_visibility(True)
                start_button.enable()

        async def finish() -> None:
            dialog.close()
            await rebuild()

        mode.on_value_change(lambda _: make_plan())
        ui.timer(0, make_plan, once=True)
    dialog.open()


async def _overlay_drift(ctx: AppContext, spec: BenchSpec, sha: str) -> list[Drift]:
    """Файлы overlay из defaults, сохранённые сервисом с другой ревизии: изменился ли с тех пор оригинал."""
    found: list[Drift] = []
    for src in sources(spec.exp.read_text(encoding="utf-8"), spec.run_dir, spec.worktree, spec.overlay):
        origin = overlay_origin(src.text) if src.origin == "overlay" else None
        if origin is None or origin[1] == sha:
            continue
        origin_rel, then_sha = origin
        now_path = spec.worktree / CONFIGS_REL / origin_rel
        then_text = await ctx.manager.file_at(then_sha, origin_rel)
        if then_text is None or not now_path.is_file():
            continue
        drift = three_way(src.rel, origin_rel, then_sha, then_text, now_path.read_text(encoding="utf-8", errors="replace"), src.text)
        if drift is not None:
            found.append(drift)
    return found


def _config_dialog(ctx: AppContext, draft: Draft, rebuild, on_overlay=None) -> None:
    ctx.access.require_admin()
    ctx.access.draft(draft.run_id)
    spec = draft.spec
    if spec is None:
        return
    exp_text = spec.exp.read_text(encoding="utf-8")
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-5xl"):
        ui.label(f"Полный конфиг · {spec.run_id}").classes("text-lg break-all")
        ui.label(f"каталог {spec.run_dir}; правки живут только здесь, worktree fleet не меняется").classes("text-xs opacity-60 break-all")
        with ui.expansion("exp.yaml — эксперимент fleet", value=True).classes("w-full"):
            editor = ui.codemirror(exp_text, language="YAML").classes("w-full").style("height: 24rem")
            exp_error = ui.label().classes("text-negative text-sm")

            async def save_exp() -> None:
                error = validate_yaml(editor.value)
                if error:
                    exp_error.set_text(error)
                    return
                exp_error.set_text("")
                spec.exp.write_text(editor.value, encoding="utf-8")
                draft.manual = True
                ui.notify("exp.yaml сохранён, пересобираю конфиг")
                dialog.close()
                await rebuild()

            ui.button("Сохранить и пересобрать", on_click=save_exp).props("unelevated no-caps dense")
        ui.label("Файлы defaults в порядке применения: побеждает последний, кто задал ключ. «Править» кладёт копию в каталог прогона.").classes("text-xs opacity-70")
        for src in sources(exp_text, spec.run_dir, spec.worktree, spec.overlay):
            _source_block(ctx, src, spec, draft, dialog, rebuild, on_overlay)
        with ui.expansion("overrides.txt — служебные overrides сервиса").classes("w-full"):
            ui.code(spec.overrides_file.read_text(encoding="utf-8") if spec.overrides_file.exists() else "", language="bash").classes("w-full")
        with ui.expansion("resolved.yaml — итоговый конфиг (--print-config)").classes("w-full"):
            if draft.resolved is None:
                ui.label(draft.resolve_error or "(ещё не собран)").classes("text-negative text-sm whitespace-pre-wrap font-mono")
            else:
                _resolved_browser(draft.resolved, exp_text, spec)
                with ui.expansion("как текст").classes("w-full").props("dense"):
                    ui.code(spec.resolved.read_text(encoding="utf-8"), language="yaml").classes("w-full").style("max-height: 32rem; overflow: auto")
        edits = manual_edits(spec.run_dir, spec.worktree, spec.overlay, exp_text)
        if edits:
            with ui.expansion(f"Diff ручных правок ({len(edits)})", value=True).classes("w-full"):
                for name, diff in edits:
                    ui.label(name).classes("text-sm")
                    ui.code(diff, language="diff").classes("w-full")
        ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
    dialog.open()


def _resolved_browser(resolved: dict[str, Any], exp_text: str, spec: BenchSpec) -> None:
    """Дерево итогового конфига; клик по ключу — откуда пришло значение: файл, строка, комментарии, интерполяция."""
    index = build_index(layers(exp_text, spec.run_dir, spec.worktree, spec.overlay), spec.service_overrides())
    ui.label("Клик по ключу — откуда пришло значение. Побеждает последний, кто задал ключ: файлы defaults по порядку, затем overrides.").classes(
        "text-xs opacity-60"
    )
    with ui.row().classes("w-full gap-4 flex-wrap items-start"):
        with ui.column().classes("flex-1 min-w-[18rem] gap-1"):
            filter_input = ui.input("фильтр ключей").props("dense clearable").classes("w-full")
            tree = ui.tree(tree_nodes(resolved), on_select=lambda e: show(e.value)).classes("w-full text-sm").props("dense")
            tree.style("max-height: 28rem; overflow: auto")
            filter_input.bind_value_to(tree, "filter")
            filter_input.on_value_change(lambda e: tree.expand() if e.value else tree.collapse())  # q-tree сам разделы под фильтр не раскрывает
        panel = ui.column().classes("flex-1 min-w-[18rem] gap-1")

    def show(key: str | None) -> None:
        panel.clear()
        if not key:
            return
        value = value_at(resolved, key)
        p = lookup(key, index, value)
        with panel:
            ui.label(key).classes("font-mono text-sm break-all")
            if not isinstance(value, (dict, list)):
                ui.label(f"итоговое значение: {value!r}").classes("text-sm break-all")
            if p.winner is None:
                ui.label(p.note).classes("text-sm text-warning")
                return
            w = p.winner
            ui.label(("вероятно: " if not p.certain else "") + w.where).classes("text-sm break-all" + ("" if p.certain else " text-warning"))
            if w.interpolation:
                ui.label(f"исходная интерполяция: {w.raw}").classes("font-mono text-xs break-all")
            if w.comments:
                ui.label(w.comments).classes("text-xs whitespace-pre-wrap opacity-80")
            if w.inline:
                ui.label(f"# {w.inline}").classes("text-xs opacity-80")
            if p.note:
                ui.label(p.note).classes("text-xs text-warning")
            snippet = _snippet(w)
            if snippet:
                ui.code(snippet, language="yaml").classes("w-full text-xs")
            if p.others:
                with ui.expansion(f"перекрытые определения ({len(p.others)})").classes("w-full").props("dense"):
                    for o in p.others:
                        ui.label(o.where + (f": {o.raw}" if o.raw and not o.container else "")).classes("text-xs break-all")


def _snippet(c: Candidate, around: int = 3) -> str:
    """Строки файла вокруг ключа с номерами; строка ключа помечена."""
    if not c.line:
        return ""
    try:
        lines = Path(c.path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    start, end = max(0, c.line - 1 - around), min(len(lines), c.line + around)
    return "\n".join(f"{'►' if i == c.line - 1 else ' '}{i + 1:4d}  {lines[i]}" for i in range(start, end))


def _source_block(ctx: AppContext, src: SourceFile, spec: BenchSpec, draft: Draft, dialog: ui.dialog, rebuild, on_overlay=None) -> None:
    drift = next((d for d in draft.drift if d.rel == src.rel), None)
    with ui.expansion(src.title + (" · оригинал изменился" if drift else "")).classes("w-full").props("dense"):
        ui.label(str(src.path)).classes("text-xs opacity-60 break-all")
        if drift is not None:
            ui.label(drift.summary + ": сравните и решите, нужна ли правка на этой ревизии").classes("text-sm text-warning")
            with ui.expansion("была → стала (ревизия)").classes("w-full").props("dense"):
                ui.code(drift.diff_original, language="diff").classes("w-full")
            with ui.expansion("была → моя правка (overlay)").classes("w-full").props("dense"):
                ui.code(drift.diff_mine, language="diff").classes("w-full")
        if src.origin == "run":
            editor = ui.codemirror(src.text, language="YAML").classes("w-full").style("height: 18rem")
            error = ui.label().classes("text-negative text-sm")

            async def save() -> None:
                problem = validate_yaml(editor.value)
                if problem:
                    error.set_text(problem)
                    return
                src.path.write_text(editor.value, encoding="utf-8")
                draft.manual = True
                dialog.close()
                await rebuild()

            with ui.row().classes("items-end gap-2 flex-wrap"):
                ui.button("Сохранить и пересобрать", on_click=save).props("unelevated no-caps dense")
                overlay_name = ui.input("имя в overlay", value=f"{src.name}-console").props("dense").classes("w-56")

                def to_overlay() -> None:
                    try:
                        target = save_to_overlay(
                            SourceFile(src.group, src.name, src.origin, src.path, editor.value), spec.overlay, spec.worktree, str(overlay_name.value or ""),
                            sha=spec.sha, run_id=spec.run_id,
                        )
                    except ValueError as exc:
                        error.set_text(str(exc))
                        return
                    error.set_text("")
                    ctx.store.event("overlay_save", spec.run_id, "ok", f"{src.rel} → {target}")
                    ui.notify(f"{src.group}/{overlay_name.value}.yaml сохранён в overlay: можно выбирать в форме и пресетах")
                    if on_overlay:
                        on_overlay()

                ui.button("Сохранить в overlay", on_click=to_overlay).props("flat no-caps dense")
                hint("Правка переезжает в overlay/configs сервиса под новым именем, с пометкой, из какого файла и ревизии она сделана. На другой ревизии сервис предупредит, если оригинал изменился.")
        else:
            ui.code(src.text, language="yaml").classes("w-full").style("max-height: 24rem; overflow: auto")

            async def edit() -> None:
                copy_for_edit(src, spec.run_dir)
                draft.manual = True
                ui.notify(f"копия {src.rel} в каталоге прогона: правьте её в редакторе")
                dialog.close()
                await rebuild()
                _config_dialog(ctx, draft, rebuild, on_overlay)  # заново, уже с редактором копии

            ui.button("Править в этом прогоне", on_click=edit).props("flat no-caps dense")


# --- карточка прогона ----------------------------------------------------------


def _run_dialog(ctx: AppContext, run_id: str, after) -> None:
    try:
        run = ctx.access.run(run_id)
    except AccessDenied as exc:
        ui.notify(str(exc), type="negative")
        return
    preview = Preview.from_json(run.preview_json)
    secrets = list(ctx.benches.secrets().values())
    spec_path = Path(run.dir) / "spec.json"
    stage_items: dict[int, ui.expansion] = {}
    last_controls = None
    last_tasks = None
    load_tasks = None

    def refresh() -> None:
        after()
        if dialog.value:
            tick()

    async def do(coro) -> None:
        try:
            await coro
        except (AccessDenied, BenchError) as exc:
            ui.notify(str(exc), type="negative", multi_line=True)
            return
        refresh()

    @ui.refreshable
    def controls(run: BenchRun, show_stop: bool) -> None:
        with ui.row().classes("gap-3 flex-wrap text-sm"):
            if run.status != "queued":
                ui.link("Argo UI по прогону", argo_ui_url(ctx.settings.argo_ui_url, ctx.settings.namespace, run_id), new_tab=True)
            if run.status == "done" and not ctx.store.request(run_id):
                ui.link("Benchboard", ctx.settings.benchboard_url, new_tab=True)
                hint("Прямых ссылок на прогон у Benchboard нет: ищите в каталоге по модели и run_id.")
            if preview.s3 and run.status != "queued":
                ui.label(f"S3: {preview.s3}").classes("opacity-70 break-all")
            ui.label(f"каталог: {run.dir}").classes("opacity-70 break-all")
        for line in ctx.benches.measure_summary(run_id):
            ui.label("замер: " + line).classes("text-sm")
        with ui.row().classes("items-end gap-2 flex-wrap"):
            if spec_path.is_file() and run.sha:
                ui.button("Запустить с теми же параметрами", on_click=repeat).props("unelevated no-caps")
                ui.button("Сохранить как пресет", on_click=save_preset).props("flat no-caps")
            if run.status == "queued":
                ui.label("Ожидает свободной квоты. Ресурсы ещё не выделены.").classes("text-sm")
                ui.button("Отменить заявку", on_click=lambda: do(ctx.benches.stop(run_id, access=ctx.access))).props("flat no-caps")
            if show_stop:
                ui.button("Остановить прогон", on_click=stop).props("flat no-caps color=negative")
            if run.status in ("attention", "failed", "stopped") and run.stage:
                ui.button(f"Повторить стадию {run.stage}", on_click=lambda: do(ctx.benches.retry(run_id, access=ctx.access))).props("unelevated no-caps")
            if run.status == "attention":
                ui.button("Погасить и остановить", on_click=stop).props("flat no-caps color=negative")
            if not run.active and run.status != "done":
                ui.button("Замер по тому, что есть", on_click=lambda: do(ctx.benches.measure(run_id, access=ctx.access))).props("flat no-caps")
            if run.status == "done":
                ui.button("Замер заново", on_click=lambda: do(ctx.benches.measure(run_id, access=ctx.access))).props("flat no-caps")
            if run.status != "queued" and (run.version_id is not None or run.session_id):
                ui.button("Лог упавшего воркера модели", on_click=model_log).props("flat dense no-caps")

    async def repeat() -> None:
        with dialog.client.layout:
            await _repeat_run(ctx, run_id, after, dialog.close)

    def save_preset() -> None:
        with dialog:
            _save_run_preset_dialog(ctx, run_id)

    def stop() -> None:
        with dialog:
            _stop_dialog(ctx, ctx.access.run(run_id), refresh)

    def model_log() -> None:
        with dialog:
            text_dialog(f"MLSC: последний упавший воркер · {run_id}", ctx.benches.model_log(run_id, access=ctx.access), ctx.benches.secrets)

    def tick() -> None:
        nonlocal run, last_controls, last_tasks, load_tasks, preview
        try:
            run = ctx.access.run(run_id)
        except AccessDenied:
            dialog.close()
            return
        status.set_text(_status_text(run))
        color = _status_color(run)
        status.classes(add=color, remove=None if color else "text-negative")
        facts.set_text(" · ".join([
            f"ревизия {run.ref}@{run.sha[:8]}" if run.ref and run.ref != run.sha else f"ревизия {run.sha[:8]}",
            f"version_id {run.version_id if run.version_id is not None else '—'}" + (f" (сессия {run.session_id})" if run.session_id else ""),
            f"начат {_when(run.started_at)}",
            f"завершён {_when(run.finished_at)}" if run.finished_at else _duration(run),
            f"requeue {run.requeues}",
        ]))
        readiness, deploy_error = _run_mlsc(ctx, run, secrets)
        replicas.set_text(readiness)
        mlsc_error.set_text(deploy_error)
        run_error.set_text(_error_text(run.error, secrets))
        remaining = _remaining_resources(ctx, run)
        warning = "После завершения прогона остались ресурсы. " + ", ".join(remaining) if remaining else ""
        if run.status == "attention":
            warning = ("После остановки можно повторить попытки с FAILED в этом прогоне." if "Модель недоступна:" in run.error
                       else "Повторите стадию или погасите модель." if run.holds_gpu
                       else "Задачи могут идти: повторите стадию или остановите прогон.")
        resources.set_text(warning)
        c = run.counters
        counters.set_text(
            f"задачи: идут {c.get('running', 0)}, ждут {c.get('pending_wf', 0)} · очередь сабмиттера {c.get('queue', '—')}, отправлено {c.get('submitted', '—')} · "
            f"завершено {c.get('completed', '—')} · сабмиттер {'жив' if c.get('submitter_alive') else 'не запущен'} · "
            f"лимит агентов {run.agent_limit if run.agent_limit is not None else '—'} · на {_when(c.get('at'))}"
            if c else ""
        )
        failed_workflows.set_text(f"Сбои workflows: {c['failed']}" if c.get("failed") else "")
        for label in (replicas, run_error, mlsc_error, resources, counters, failed_workflows):
            label.set_visibility(bool(label.text))

        if preview.tasks_total is None and (total := tasks_total(ctx.paths.runs / run_id)) is not None:
            preview = replace(preview, tasks_total=total)
        parameters.set_text("Параметры заявки" if run.status == "queued" else "Что и где запущено")
        rows = [{"k": k, "v": v} for k, v in preview.rows()] if run.preview_json != "{}" else []
        ids = f"run_id {run.id} · лейбл Argo fleet/run-name={run.id}"
        if run.version_id is not None:
            ids += f" · MLSC version_id {run.version_id}"
        rows.append({"k": "Идентификаторы", "v": ids})
        if detail_table.rows != rows:
            detail_table.update_rows(rows)
        show_stop = run.status in ("running", "stopping") or bool(remaining)
        control_state = (run.status, run.stage, show_stop, run.version_id)
        if last_controls is None:
            with actions:
                controls(run, show_stop)
        elif control_state != last_controls:
            controls.refresh(run, show_stop)
        last_controls = control_state

        if run.status != "queued":
            task_state = (run.stage, run.active, *(c.get(key) for key in ("running", "pending_wf", "succeeded", "failed", "submitted", "completed")))
            with task_panel:
                if load_tasks is None:
                    load_tasks = _tasks_section(ctx, run, preview.s3)
                    ui.timer(0, load_tasks, once=True)
                elif task_state != last_tasks and (run.stage in {"execute", "requeue"} or run.active != last_tasks[1]):
                    ui.timer(0, load_tasks, once=True)
            last_tasks = task_state
        if load_tasks is not None:
            task_panel.set_visibility(run.status != "queued")

        stages = ctx.benches.stages(run_id)
        no_stages.set_visibility(not stages)
        for st in stages:
            result = "идёт" if st.finished_at is None else ("прервана" if st.returncode is None else f"код {st.returncode}")
            title = f"{STAGE_TITLES.get(st.stage, st.stage)} · {result} · {_when(st.started_at)}"
            if st.id not in stage_items:
                with stage_panel, ui.expansion(title).classes("w-full").props("dense") as item:
                    ui.code(scrub(st.command, secrets), language="bash").classes("w-full")
                    if st.log_path:
                        ui.button("Хвост лога", on_click=lambda p=st.log_path: _run_log(ctx, run_id, p, secrets)).props("flat dense no-caps")
                stage_items[st.id] = item
            stage_items[st.id].set_text(title)
            if st.finished_at and st.returncode not in (0, None):
                stage_items[st.id].classes("text-negative")
        events = [e for e in ctx.store.events(300) if e.object == run_id][:15]
        journal.set_visibility(bool(events))
        journal.set_text(f"Журнал ({len(events)})")
        event_text.set_text("\n".join(
            _error_text(f"{_when(e.ts)} {e.action} {e.result} {e.details}", secrets) for e in events
        ))

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl bench-run-card"):
        ui.label(run.id).classes("text-lg break-all")
        if ctx.settings.auth.mode != "local":
            owner, coauthors = ctx.store.run_ownership(run.id)

            def name(user_id: str) -> str:
                account = ctx.store.user(user_id)
                return (account.display_name if account else "") or user_id

            byline = f"Владелец: {name(owner)}"
            if coauthors:
                byline += " · Соавторы: " + ", ".join(map(name, coauthors))
            ui.label(byline).classes("text-sm break-all")
        status = ui.label().classes("font-medium")
        facts = ui.label().classes("text-sm")
        replicas = ui.label().classes("text-sm")
        mlsc_error = ui.label().classes("text-negative text-sm whitespace-pre-wrap break-words w-full")
        run_error = ui.label().classes("text-negative text-sm whitespace-pre-wrap break-words w-full")
        resources = ui.label().classes("text-negative text-sm")
        if spec_path.is_file():
            origin = BenchSpec.from_json(spec_path.read_text(encoding="utf-8")).dataset_source
            if origin:
                ui.label(f"Данные из локальной папки: {origin['source']} · загружены в S3 {origin.get('uploaded_at', '')}").classes("text-sm break-all")
        counters = ui.label().classes("text-sm")
        failed_workflows = ui.label().classes("text-negative text-sm")
        with ui.expansion(value=run.status == "running").classes("w-full") as parameters:
            detail_table = _facts_table([]).mark("bench-facts")
        actions = ui.column().classes("w-full")
        task_panel = ui.column().classes("w-full")
        ui.label("Стадии").classes("mt-2")
        no_stages = ui.label("ещё не начинались").classes("text-sm opacity-70")
        stage_panel = ui.column().classes("w-full gap-0")
        with ui.expansion().classes("w-full").props("dense") as journal:
            event_text = ui.label().classes("font-mono text-xs whitespace-pre-wrap break-words w-full")
        ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
        tick()
        ui.timer(3.0, tick, immediate=False)
    dialog.on("hide", dialog.delete)
    dialog.open()


def _tasks_section(ctx: AppContext, run: BenchRun, s3_base: str) -> Callable[[], Awaitable[None]]:
    """Таблица задач прогона: фазы workflows Argo и результаты в S3 (попытки tryN, флаг COMPLETED/FAILED)."""
    state: dict[str, Any] = {"rows": [], "results": {}, "tasks": {}}
    ui.label("Задачи").classes("mt-2")
    with ui.row().classes("items-center gap-3 flex-wrap"):
        only_failed = ui.checkbox("только упавшие").props("dense")
        ui.button("Обновить", on_click=lambda: load()).props("flat dense no-caps")
        if s3_base:
            ui.button("Файлы в S3", on_click=lambda: _s3_dialog(ctx, run, s3_base)).props("flat dense no-caps")
        retry = ui.button("Повторить неудачные попытки", on_click=lambda: confirm_retry()).props("unelevated no-caps")
        retry.set_visibility(False)

    def confirm_retry() -> None:
        selected = failed_selection(state["results"])
        current = ctx.access.run(run.id)
        if current.active or not selected:
            return
        request = ctx.store.request(run.id)
        confirm_dialog("Повторить неудачные попытки?", [
            f"Прогон {run.id} · последних попыток с FAILED: {len(selected)}.",
            (f"Будет поднята версия MLSC {current.version_id}, резерв {request['gpu']} GPU."
             if current.own_model else "Будет использована готовая сессия модели."),
            "Результаты новых попыток появятся в том же прогоне. После исполнения замер будет пересчитан.",
        ], lambda: ctx.benches.retry_failed(run.id, selected, access=ctx.access), button="Повторить")

    note = ui.label("читаю workflows…").classes("text-xs opacity-70")
    table = ui.table(columns=TASK_COLUMNS, rows=[], row_key="task").classes("w-full").props("dense flat").mark("bench-tasks")
    table.on("rowClick", lambda e: _task_dialog(ctx, run, s3_base, e.args[1], state))

    def render() -> None:
        table.rows = [r for r in state["rows"] if not only_failed.value or r["failed"]]
        table.update()

    lock = asyncio.Lock()

    async def load() -> None:
        if lock.locked():
            return
        async with lock:
            await fetch()

    async def fetch() -> None:
        retry.set_visibility(False)
        now = datetime.now(timezone.utc)
        try:
            tasks = await ctx.benches.task_workflows(run.id, access=ctx.access)
        except Exception as exc:  # noqa: BLE001 — kubectl недоступен: показываем причину
            note.set_text(f"Argo: {exc}")
            tasks = []
        state["tasks"] = {t.task: t for t in tasks}
        state["rows"] = _task_rows(tasks, {}, now)
        render()
        note.set_text(f"workflows: {len(tasks)} · S3: читаю…" if s3_base else f"workflows: {len(tasks)}")
        if not s3_base:
            return
        try:
            results = scan_results(await ctx.benches.s3_objects(run.id, access=ctx.access))
        except Exception as exc:  # noqa: BLE001 — нет ключей или aws: таблица остаётся по Argo
            note.set_text(f"workflows: {len(tasks)} · S3: {exc}")
            return
        current = ctx.access.run(run.id)
        failures = len(failed_selection(results))
        if not current.active:
            ctx.store.update_bench(run.id, failed_attempts=failures)
            retry.set_visibility(bool(failures and current.version_id and ctx.store.request(run.id)))
        state["results"] = results
        state["rows"] = _task_rows(tasks, results, now)
        render()
        done = sum(1 for r in results.values() if r.status == "completed")
        note.set_text(f"workflows: {len(tasks)} · задач в S3: {len(results)}, COMPLETED {done}, FAILED {sum(1 for r in results.values() if r.status == 'failed')}")

    only_failed.on_value_change(lambda _: render())
    return load


def _task_rows(tasks: list[TaskWorkflow], results: dict[str, TaskResult], now: datetime) -> list[dict[str, Any]]:
    by_task: dict[str, dict[str, Any]] = {}
    for t in tasks:  # несколько workflows одной задачи (повторный сабмит) — последняя по времени
        d = t.duration(now)
        by_task[t.task] = {
            "task": t.task, "phase": PHASE_TEXT.get(t.phase, t.phase), "name": t.name, "rel": t.results_rel, "duration": _hms(d), "failed": t.failed,
            "message": t.message, "active": t.active, "result": "",
        }
    for rel, r in results.items():
        row = by_task.setdefault(r.task, {"task": r.task, "phase": "—", "name": "", "rel": rel, "duration": "", "failed": False, "message": "", "active": False})
        row["rel"] = row["rel"] or rel
        row["result"] = f"{r.tries} · {RESULT_TEXT.get(r.status, r.status)}"
        row["failed"] = row["failed"] or r.status == "failed"
    return sorted(by_task.values(), key=lambda r: r["task"])


def _hms(d: timedelta | None) -> str:
    if d is None:
        return ""
    total = int(d.total_seconds())
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _task_dialog(ctx: AppContext, run: BenchRun, s3_base: str, row: dict[str, Any], state: dict[str, Any]) -> None:
    """Одна задача: workflow в Argo, логи контейнеров живого пода, файлы последней попытки в S3."""
    ctx.access.run(run.id)
    task = str(row.get("task", ""))
    wf: TaskWorkflow | None = state["tasks"].get(task)
    result: TaskResult | None = state["results"].get(row.get("rel", ""))
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-3xl"):
        ui.label(task).classes("text-lg break-all")
        with ui.row().classes("gap-3 flex-wrap text-sm items-center"):
            if wf is not None:
                ui.label(f"Argo: {PHASE_TEXT.get(wf.phase, wf.phase)} · {wf.name}")
                ui.link("открыть в Argo UI", workflow_url(ctx.settings.argo_ui_url, ctx.settings.namespace, wf.name), new_tab=True)
                if wf.message:
                    ui.label(wf.message).classes("text-negative break-all")
                if wf.active:
                    for container in ("main", "dind-daemon"):
                        ui.button(f"лог {container}", on_click=lambda c=container: text_dialog(f"{wf.name} · {c}", ctx.benches.task_log(wf.name, c, run_id=run.id, access=ctx.access), ctx.benches.secrets)).props("flat dense no-caps")
            else:
                ui.label("workflow в Argo не виден (удалён или ещё не создан)").classes("opacity-70")
        if result is None:
            ui.label("в S3 результатов нет" if s3_base else "путь S3 неизвестен").classes("text-sm opacity-70")
        else:
            n = result.latest_try
            ui.label(f"S3: попыток {n} · итог {RESULT_TEXT.get(result.status, result.status)} · {s3_base.rstrip('/')}/{result.rel}").classes("text-sm break-all")
            for obj in result.files:
                uri = f"{s3_base.rstrip('/')}/{result.rel}/try{n}/{obj.key}"
                with ui.row().classes("items-center gap-2 no-wrap text-xs"):
                    ui.label(obj.key).classes("break-all")
                    ui.label(_size(obj.size)).classes("opacity-60")
                    if obj.is_text and obj.size:
                        ui.button("показать", on_click=lambda u=uri: text_dialog(u, ctx.benches.s3_text(u, run_id=run.id, access=ctx.access), ctx.benches.secrets)).props("flat dense no-caps")
        ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
    dialog.open()


def _s3_dialog(ctx: AppContext, run: BenchRun, s3_base: str) -> None:
    """Все объекты прогона в S3 с фильтром; текстовые можно открыть."""
    ctx.access.run(run.id)
    files: dict[str, S3Object] = {}
    columns = [
        {"name": "key", "label": "Ключ", "field": "key", "align": "left", "sortable": True},
        {"name": "size", "label": "Размер", "field": "size", "align": "right", "sortable": True},
        {"name": "modified", "label": "Изменён", "field": "modified", "align": "left"},
    ]
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label(s3_base).classes("text-sm break-all")
        note = ui.label("читаю…").classes("text-xs opacity-70")
        with ui.row().classes("items-center gap-3"):
            filter_input = ui.input("фильтр").props("dense clearable").classes("w-64")
        table = ui.table(columns=columns, rows=[], row_key="key").classes("w-full").props("dense flat")
        table.style("max-height: 28rem")
        filter_input.bind_value_to(table, "filter")
        table.on("rowClick", lambda e: _open_if_text(ctx, run.id, files.get(e.args[1]["key"])))
        ui.label("клик по строке текстового файла — показать начало").classes("text-xs opacity-60")
        ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
    dialog.open()

    async def load() -> None:
        try:
            objects = await ctx.benches.s3_objects(run.id, access=ctx.access)
        except Exception as exc:  # noqa: BLE001
            note.set_text(str(exc))
            note.classes("text-negative")
            return
        files.update((o.key, o) for o in objects)
        table.rows = [{"key": o.key, "size": _size(o.size), "modified": o.modified, "text": o.is_text and o.size > 0} for o in objects]
        table.update()
        note.set_text(f"объектов {len(objects)} · {_size(sum(o.size for o in objects))}")

    ui.timer(0, load, once=True)


def _open_if_text(ctx: AppContext, run_id: str, obj: S3Object | None) -> None:
    ctx.access.run(run_id)
    if obj is None or not obj.is_text or obj.size == 0:
        ui.notify("не текстовый файл или пустой")
        return
    uri = f"{ctx.benches.s3_base(run_id).rstrip('/')}/{obj.key}"
    text_dialog(uri, ctx.benches.s3_text(uri, run_id=run_id, access=ctx.access), ctx.benches.secrets)


def _size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ГБ"


def _duration(run: BenchRun) -> str:
    if not run.started_at:
        return "не начат"
    minutes = int((datetime.now(timezone.utc) - datetime.fromisoformat(run.started_at)).total_seconds() // 60)
    return f"идёт {minutes // 60} ч {minutes % 60} мин" if minutes >= 60 else f"идёт {minutes} мин"


def _stop_dialog(ctx: AppContext, run: BenchRun, after) -> None:
    c = run.counters
    lines = [
        f"задач идёт {c.get('running', 0)}, в очереди {c.get('queue', '—')}: лимит агентов станет 0, сабмиттер прогона будет убит, активным workflows уйдёт Terminate",
        f"модель: версия {run.version_id} будет погашена (mlsc.stop_llm)" if run.own_model and run.version_id is not None
        else ("модель сессии не гасится" if run.session_id else "если найдётся созданная этим прогоном версия MLSC, она будет погашена"),
        "стадия, если идёт, будет убита",
    ]
    confirm_dialog(f"Остановить прогон {run.id}?", lines, lambda: ctx.benches.stop(run.id, access=ctx.access), after, button="Остановить")


def _run_log(ctx: AppContext, run_id: str, path: str, secrets: list[str]) -> None:
    ctx.access.run(run_id)
    _log_tail_dialog(path, secrets)


def _log_tail_dialog(path: str, secrets: list[str], lines: int = 200) -> None:
    try:
        text = "\n".join(Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]) or "(пусто)"
    except OSError as exc:
        text = f"лог не прочитать: {exc}"
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-5xl"):
        ui.label(path).classes("text-xs opacity-60 break-all")
        ui.label(scrub(text, secrets)).classes("font-mono text-xs whitespace-pre-wrap w-full").style("max-height: 70vh; overflow: auto")
        ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
    dialog.open()

