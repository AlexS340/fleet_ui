"""Страница «Прогоны»: форма запуска бенча из пресета, превью «что и где» и preflight, карточка прогона.

Одно главное действие — «Запустить бенч». Детали по раскрытию: форма → «Дополнительно» (overrides,
harbor_job) → «Полный конфиг» (exp.yaml, файлы defaults с комментариями и правкой, resolved.yaml).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import Any

from nicegui import ui
import yaml

from fleet_console.compose import (
    BenchSpec,
    Check,
    Preview,
    SourceFile,
    build_registry,
    copy_for_edit,
    from_resolved,
    load_issues,
    make_bench_id,
    manual_edits,
    run_checks,
    sources,
    summary,
    validate_yaml,
)
from fleet_console.context import AppContext
from fleet_console.fleetref.manager import ReadyRevision
from fleet_console.fleetref.profile import list_groups
from fleet_console.models.compose import recipe_summary
from fleet_console.observe.argo import argo_ui_url
from fleet_console.presets import DatasetRef, compatibility, load_presets
from fleet_console.runs import STAGE_TITLES, BenchError, dataset_of
from fleet_console.settings import OVERLAY_DIR, PRESETS_DIR, scrub
from fleet_console.store import BenchRun
from fleet_console.ui.common import confirm_dialog, hint
from fleet_console.ui.header import build_header

STATUS_TEXT = {"running": "идёт", "attention": "требует внимания", "stopping": "останавливается", "stopped": "остановлен", "failed": "не удался", "done": "готов"}
LEVEL_ICON = {"ok": ("check_circle", "text-positive"), "warn": ("warning", "text-warning"), "fail": ("cancel", "text-negative")}
COLUMNS = [
    {"name": "id", "label": "Прогон", "field": "id", "align": "left", "sortable": True},
    {"name": "model", "label": "Модель", "field": "model", "align": "left"},
    {"name": "dataset", "label": "Датасет", "field": "dataset", "align": "left"},
    {"name": "status", "label": "Статус", "field": "status", "align": "left"},
    {"name": "tasks", "label": "Задачи: идут / очередь / готово", "field": "tasks", "align": "left"},
    {"name": "limit", "label": "Лимит", "field": "limit", "align": "right"},
    {"name": "started", "label": "Начат", "field": "started", "align": "left"},
]
CUSTOM_DATASET = "__custom__"
OWN_MODEL_PREFIX = "recipe:"
SESSION_PREFIX = "session:"


def _when(text: str | None) -> str:
    return datetime.fromisoformat(text).astimezone().strftime("%d.%m %H:%M") if text else "—"


def _status_text(run: BenchRun) -> str:
    text = STATUS_TEXT.get(run.status, run.status)
    return f"{text}: {STAGE_TITLES.get(run.stage, run.stage)}" if run.status == "running" and run.stage else text


def _revision_title(revision: ReadyRevision) -> str:
    ref = revision.ref if revision.ref != revision.info.sha else revision.info.sha[:8]
    merged = "влита в master" if revision.info.merged_into_main else f"не влита, отстаёт на {revision.info.behind_main}"
    return f"{ref}@{revision.info.sha[:8]} · {merged}"


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


# --- страница --------------------------------------------------------------


async def benches_page(ctx: AppContext) -> None:
    build_header(ctx)
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            ui.label("Прогоны").classes("text-xl")
            hint("Бенч — прогон задач датасета агентом на модели: стадии fleet от материализации до замера в Benchboard.")
            ui.space()
            ui.button("Запустить бенч", on_click=lambda: _new_bench_dialog(ctx, render)).props("unelevated no-caps")
        banner = ui.label().classes("w-full text-negative text-sm whitespace-pre-wrap")
        table = ui.table(columns=COLUMNS, rows=[], row_key="id").classes("w-full").props("dense flat")
        table.on("rowClick", lambda e: _run_dialog(ctx, e.args[1]["id"], render))
        empty = ui.label("Прогонов ещё нет. «Запустить бенч» собирает exp.yaml из пресета, показывает превью и preflight, затем ведёт стадии fleet.").classes(
            "text-sm opacity-70"
        )
        ui.label("клик по строке — карточка прогона: что и где запущено, стадии с командами, логи, остановка и повтор").classes("text-xs opacity-60")

    def render() -> None:
        runs = ctx.benches.runs()
        rows = []
        for r in runs[:50]:
            c = r.counters
            tasks = f"{c.get('running', 0)} / {c.get('queue') if c.get('queue') is not None else '—'} / {c.get('completed') if c.get('completed') is not None else '—'}"
            rows.append(
                {
                    "id": r.id,
                    "model": r.model or r.recipe,
                    "dataset": dataset_of(r) if isinstance(dataset_of(r), str) else dataset_of(r).label,
                    "status": _status_text(r),
                    "tasks": tasks if r.status != "running" or r.stage == "execute" or c else "—",
                    "limit": r.agent_limit if r.agent_limit is not None else "—",
                    "started": _when(r.started_at),
                }
            )
        table.update_rows(rows)
        table.set_visibility(bool(rows))
        empty.set_visibility(not rows)
        attention = [r for r in runs if r.status == "attention"]
        lines = [
            f"⚠ {r.id}: {'модель поднята, GPU тратятся' if r.holds_gpu else 'задачи могут идти'} — {r.error}"
            + (f" · автогашение через {ctx.settings.bench.attention_minutes} мин без ответа" if r.holds_gpu and ctx.settings.bench.attention_minutes else "")
            for r in attention
        ]
        banner.set_text("\n".join(lines))
        banner.set_visibility(bool(lines))

    render()
    ui.timer(3.0, render)


def _facts_table(rows: list[dict[str, str]]) -> ui.table:
    columns = [{"name": "k", "field": "k", "align": "left", "style": "width: 11rem; white-space: nowrap"}, {"name": "v", "field": "v", "align": "left"}]
    return ui.table(columns=columns, rows=rows).props("dense flat hide-header wrap-cells").classes("w-full")


# --- форма запуска -------------------------------------------------------------


def _new_bench_dialog(ctx: AppContext, after) -> None:  # noqa: C901 — форма целиком, чтобы состояние жило в замыкании
    revision = ctx.manager.current
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label("Запустить бенч").classes("text-lg")
        if revision is None or not revision.profile.compatible:
            ui.label("Сначала подготовьте совместимую ревизию fleet на главной странице: прогон идёт её шагами.").classes("text-sm")
            ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
            dialog.open()
            return
        ui.label(f"ревизия {_revision_title(revision)}").classes("text-sm opacity-70")
        groups = merged_groups(revision)
        overlay = overlay_members()
        presets, preset_errors = load_presets(PRESETS_DIR)
        draft = Draft()

        def label_of(group: str, member: str) -> str:
            return f"{member} (overlay)" if member in overlay.get(group, set()) else member

        # --- поля формы
        preset_options = {"": "— без пресета —"} | {p.name: f"{p.name} · {p.description}" if p.description else p.name for p in presets}
        preset_select = ui.select(preset_options, value="", label="Пресет").classes("w-full")
        preset_note = ui.label().classes("text-xs whitespace-pre-wrap")
        if preset_errors:
            ui.label("битые пресеты: " + "; ".join(preset_errors)).classes("text-xs text-negative")

        model_options: dict[str, str] = {}
        for recipe in groups.get("mlsc/model", []):
            summary_ = recipe_summary(revision.worktree, recipe) if recipe not in overlay.get("mlsc/model", set()) else {}
            gpu = summary_.get("gpu_per_instance")
            model_options[OWN_MODEL_PREFIX + recipe] = f"поднять: {label_of('mlsc/model', recipe)}" + (f" · {gpu} GPU/инстанс" if gpu else "")
        for s in ctx.sessions.sessions(active_only=True):
            if s.status == "ready":
                model_options[SESSION_PREFIX + s.id] = f"сессия: {s.model} · {s.replicas} реплик · версия {s.version_id}"
        with ui.row().classes("w-full gap-3 flex-wrap items-end"):
            model_select = ui.select(model_options, value=next(iter(model_options), None), label="Модель", with_input=True).classes("flex-1 min-w-[18rem]")
            model_name = ui.input("served_model_name (пусто — из рецепта)").classes("flex-1 min-w-[14rem]")
        with ui.row().classes("w-full gap-3 flex-wrap items-end"):
            agents = groups.get("harbor/agent", [])
            agent_select = ui.select({a: label_of("harbor/agent", a) for a in agents}, value=agents[0] if agents else None, label="Агент · harbor/agent", with_input=True).classes(
                "flex-1 min-w-[16rem]"
            )
            datasets = groups.get("harbor/dataset", [])
            dataset_options = {d: label_of("harbor/dataset", d) for d in datasets} | {CUSTOM_DATASET: "свой путь под FLEET_DATASETS_ROOT…"}
            dataset_select = ui.select(dataset_options, value=datasets[0] if datasets else CUSTOM_DATASET, label="Датасет · harbor/dataset", with_input=True).classes(
                "flex-1 min-w-[16rem]"
            )
        with ui.row().classes("w-full gap-3 flex-wrap items-end") as custom_row:
            custom_path = ui.input("путь датасета относительно FLEET_DATASETS_ROOT").classes("flex-1 min-w-[14rem]")
            custom_benchmark = ui.input("benchmark").classes("w-40")
            custom_mapping = ui.select({"": "маппинг по tasks_root"} | {m: m for m in groups.get("datasets", [])}, value="", label="реестр datasets").classes("w-56")
        with ui.row().classes("gap-3 flex-wrap items-end"):
            jobs = ui.number("Реплики", value=1, min=1, precision=0).classes("w-28")
            apr = ui.number("Агентов на реплику", value=1, min=1, precision=0).classes("w-40")
            task_limit = ui.number("Лимит задач (пусто — все)", value=None, min=1, precision=0).classes("w-44")
            profile = ui.select(groups.get("execution", ["production"]), value="production" if "production" in groups.get("execution", []) else None, label="Профиль").classes("w-36")
            inference = ui.select({"litellm": "litellm", "nginx": "nginx"}, value="litellm", label="Маршрут").classes("w-28")
        gpu_label = ui.label().classes("text-sm")
        with ui.expansion("Дополнительно").classes("w-full"):
            extra = ui.textarea("overrides fleet, по одному на строку: ключ=значение").classes("w-full").props("autogrow")
            harbor_job_text = ui.textarea("harbor_job поверх (YAML): таймауты, retry…").classes("w-full").props("autogrow")
            config_button = ui.button("Полный конфиг: файлы, комментарии, правка", on_click=lambda: _config_dialog(ctx, draft, rebuild)).props("flat no-caps")
            config_button.disable()
        preview_box = ui.column().classes("w-full gap-1")
        checks_box = ui.column().classes("w-full gap-1")
        accept = ui.checkbox("Принимаю предупреждения").mark("accept-warnings")
        accept.set_visibility(False)
        with ui.row().classes("gap-2 items-center"):
            preview_button = ui.button("Превью и preflight", on_click=lambda: rebuild()).props("unelevated no-caps")
            start_button = ui.button("Запустить", on_click=lambda: ask()).props("unelevated no-caps color=positive").mark("start-bench")
            start_button.disable()
            ui.button("Отмена", on_click=dialog.close).props("flat no-caps")
            busy = ui.spinner(size="sm")
            busy.set_visibility(False)

        # --- поведение
        def is_session() -> bool:
            return str(model_select.value or "").startswith(SESSION_PREFIX)

        def refresh_gpu() -> None:
            custom_row.set_visibility(dataset_select.value == CUSTOM_DATASET)
            model_name.set_visibility(not is_session())
            n = int(jobs.value or 1)
            if is_session():
                s = ctx.sessions.get(model_select.value[len(SESSION_PREFIX):])
                gpu_label.set_text(f"модель уже поднята: сессия {s.model}, реплик {s.replicas}; подъём и гашение пропускаются, таймер сессии стоит")
                jobs.set_value(s.replicas)
                jobs.disable()
                return
            jobs.enable()
            recipe = str(model_select.value or "")[len(OWN_MODEL_PREFIX):]
            gpu = recipe_summary(revision.worktree, recipe).get("gpu_per_instance") if recipe else None
            gpu_label.set_text(f"GPU: {n} × {gpu} = {n * gpu} · лимит агентов {n} × {int(apr.value or 1)} = {n * int(apr.value or 1)}" if gpu else f"лимит агентов {n * int(apr.value or 1)}")

        def apply_preset(name: str | None) -> None:
            preset = next((p for p in presets if p.name == name), None)
            if preset is None:
                preset_note.set_text("")
                return
            problems = compatibility(preset, groups, revision.profile.steps, [])
            preset_note.set_text(("несовместим с ревизией: " + "; ".join(problems)) if problems else f"пресет под {preset.fleet.ref}; поля ниже заполнены из него")
            preset_note.classes(replace="text-xs whitespace-pre-wrap " + ("text-negative" if problems else "opacity-70"))
            key = OWN_MODEL_PREFIX + preset.model
            if key in model_options:
                model_select.set_value(key)
            model_name.set_value(preset.model_name)
            if preset.agent in agent_select.options:
                agent_select.set_value(preset.agent)
            if isinstance(preset.dataset, str):
                if preset.dataset in dataset_select.options:
                    dataset_select.set_value(preset.dataset)
            else:
                dataset_select.set_value(CUSTOM_DATASET)
                custom_path.set_value(preset.dataset.path)
                custom_benchmark.set_value(preset.dataset.benchmark)
                custom_mapping.set_value(preset.dataset.mapping or "")
            jobs.set_value(preset.resources.jobs)
            apr.set_value(preset.resources.agents_per_replica)
            task_limit.set_value(preset.execution.task_limit)
            if preset.execution.profile in profile.options:
                profile.set_value(preset.execution.profile)
            inference.set_value(preset.inference)
            extra.set_value("\n".join(preset.overrides))
            harbor_job_text.set_value(yaml.safe_dump(preset.harbor_job, allow_unicode=True, sort_keys=False) if preset.harbor_job else "")
            refresh_gpu()

        def dataset_value() -> str | DatasetRef:
            if dataset_select.value == CUSTOM_DATASET:
                return DatasetRef(path=(custom_path.value or "").strip(), benchmark=(custom_benchmark.value or "").strip(), mapping=(custom_mapping.value or None))
            return str(dataset_select.value)

        def build_spec() -> BenchSpec:
            dataset = dataset_value()
            if isinstance(dataset, DatasetRef) and not (dataset.path and dataset.benchmark):
                raise BenchError("для своего датасета нужны путь и benchmark")
            if not model_select.value or not agent_select.value:
                raise BenchError("выберите модель и агента")
            try:
                harbor_job = yaml.safe_load(harbor_job_text.value or "") or {}
            except yaml.YAMLError as exc:
                raise BenchError(f"harbor_job: не YAML — {exc}") from exc
            if not isinstance(harbor_job, dict):
                raise BenchError("harbor_job должен быть словарём")
            session = None
            recipe = str(model_select.value)[len(OWN_MODEL_PREFIX):]
            if is_session():
                session = ctx.benches.session_link(str(model_select.value)[len(SESSION_PREFIX):])
                recipe = session.recipe
            preset_name = str(preset_select.value or "")
            if not draft.run_id:
                base = preset_name or f"{session.model if session else recipe}-{dataset if isinstance(dataset, str) else dataset.benchmark}"
                draft.run_id = make_bench_id(base, datetime.now(timezone.utc))
            return BenchSpec(
                run_id=draft.run_id, worktree=revision.worktree, run_dir=ctx.paths.runs / draft.run_id, overlay=OVERLAY_DIR, recipe=recipe,
                agent=str(agent_select.value), dataset=dataset, jobs=int(jobs.value or 1), agents_per_replica=int(apr.value or 1),
                task_limit=int(task_limit.value) if task_limit.value else None, profile=str(profile.value or "production"), inference=str(inference.value),
                model_name=(model_name.value or "").strip() if not session else "", harbor_job=harbor_job,
                extra=tuple(line.strip() for line in (extra.value or "").splitlines() if line.strip()), pod=draft.spec.pod if draft.spec else "",
                session=session, preset=preset_name, sha=revision.info.sha,
            )

        async def rebuild() -> None:
            """Черновик: exp.yaml → реестр → --print-config → превью и preflight."""
            busy.set_visibility(True)
            preview_button.disable()
            try:
                spec = build_spec()
                if not spec.pod:
                    spec = BenchSpec(**{**spec.__dict__, "pod": await ctx.benches._pod()})
                spec.write(force=not draft.manual)
                draft.spec = spec
                mappings, registry_errors = build_registry(spec.worktree, spec.overlay, spec.run_dir)
                draft.resolved, draft.resolve_error = await ctx.benches.print_config(spec)
                session_status = ctx.sessions.get(spec.session.id).status if spec.session else None
                draft.preview = from_resolved(
                    draft.resolved or {}, revision=_revision_title(revision), context=ctx.kube_status.context or "", run_dir=spec.run_dir,
                    session=spec.session.id if spec.session else "",
                )
                foreign = {v.model for v in ctx.observer.mlsc.data or [] if not any(o.owns_version(v.version_id, v.application) for o in ctx.store.own_runs().values())}
                draft.checks = run_checks(
                    secrets=ctx.benches.secrets(), kube=ctx.kube_status, profile=revision.profile, own_model=spec.session is None, resolved=draft.resolved,
                    resolve_error=draft.resolve_error, datasets_root=ctx.settings.datasets_root_path(), mappings=mappings, registry_errors=registry_errors,
                    foreign_models=foreign, model_name=spec.effective_model or draft.preview.model, agent=spec.agent, recipe=spec.recipe,
                    dataset=spec.dataset_label, issues=load_issues(), session_status=session_status,
                )
            except BenchError as exc:
                ui.notify(str(exc), type="negative", multi_line=True)
                return
            finally:
                busy.set_visibility(False)
                preview_button.enable()
            config_button.enable()
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
                    with ui.row().classes("items-start gap-2 no-wrap"):
                        ui.icon(icon).classes(f"{color} text-base mt-0.5")
                        ui.label(c.title).classes("text-sm")
                        ui.label(c.detail).classes("text-xs opacity-70 break-all")
            accept.set_visibility(warns > 0)
            accept.set_value(False)
            refresh_start()

        def refresh_start() -> None:
            fails, warns = summary(draft.checks)
            ok = draft.spec is not None and draft.resolved is not None and fails == 0 and (warns == 0 or bool(accept.value))
            start_button.enable() if ok else start_button.disable()

        def ask() -> None:
            spec, preview = draft.spec, draft.preview
            if spec is None or preview is None:
                return
            gpu = preview.gpu_total
            stages = ("preflight → материализация → " + ("" if spec.session else "create_job → start_llm → check → ") + "upload → launch → исполнение"
                      + (f" → requeue ×{ctx.settings.bench.requeue_rounds}" if ctx.settings.bench.requeue_rounds else "") + ("" if spec.session else " → stop_llm") + " → замер")
            lines = [
                f"{preview.model} × {spec.dataset_label} × {spec.agent}; реплик {preview.replicas}, агентов на реплику {spec.agents_per_replica}",
                f"GPU: {gpu if gpu is not None else '?'}" + (" — модель уже поднята (сессия)" if spec.session else " — сервис поднимет и погасит модель сам"),
                f"задач: {preview.tasks_total if preview.tasks_total is not None else 'узнаем после материализации'}" + (f", лимит {spec.task_limit}" if spec.task_limit else ""),
                "стадии: " + stages,
                f"результаты: {preview.s3}",
            ]

            async def start() -> None:
                run = await ctx.benches.start(spec, ref=revision.ref, preview=preview)
                dialog.close()
                ui.notify(f"Прогон {run.id} начат")

            confirm_dialog("Запустить прогон?", lines, start, after, button="Да, запустить")

        def discard_draft() -> None:
            """Форма закрыта без запуска: каталог черновика (exp.yaml, resolved.yaml) больше не нужен."""
            if draft.spec is not None and ctx.store.get_bench(draft.spec.run_id) is None and draft.spec.run_dir.is_dir():
                shutil.rmtree(draft.spec.run_dir, ignore_errors=True)

        preset_select.on_value_change(lambda e: apply_preset(e.value))
        for element in (model_select, dataset_select, jobs, apr):
            element.on_value_change(lambda _: refresh_gpu())
        accept.on_value_change(lambda _: refresh_start())
        dialog.on("hide", lambda _: discard_draft())
        refresh_gpu()
    dialog.open()


# --- полный конфиг ---------------------------------------------------------------


def _config_dialog(ctx: AppContext, draft: Draft, rebuild) -> None:
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
            _source_block(ctx, src, spec, draft, dialog, rebuild)
        with ui.expansion("overrides.txt — служебные overrides сервиса").classes("w-full"):
            ui.code(spec.overrides_file.read_text(encoding="utf-8") if spec.overrides_file.exists() else "", language="bash").classes("w-full")
        with ui.expansion("resolved.yaml — итоговый конфиг (--print-config)").classes("w-full"):
            text = spec.resolved.read_text(encoding="utf-8") if spec.resolved.exists() else draft.resolve_error or "(ещё не собран)"
            ui.code(text, language="yaml").classes("w-full").style("max-height: 32rem; overflow: auto")
        edits = manual_edits(spec.run_dir, spec.worktree, spec.overlay, exp_text)
        if edits:
            with ui.expansion(f"Diff ручных правок ({len(edits)})", value=True).classes("w-full"):
                for name, diff in edits:
                    ui.label(name).classes("text-sm")
                    ui.code(diff, language="diff").classes("w-full")
        ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
    dialog.open()


def _source_block(ctx: AppContext, src: SourceFile, spec: BenchSpec, draft: Draft, dialog: ui.dialog, rebuild) -> None:
    with ui.expansion(src.title).classes("w-full").props("dense"):
        ui.label(str(src.path)).classes("text-xs opacity-60 break-all")
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

            ui.button("Сохранить и пересобрать", on_click=save).props("unelevated no-caps dense")
        else:
            ui.code(src.text, language="yaml").classes("w-full").style("max-height: 24rem; overflow: auto")

            async def edit() -> None:
                copy_for_edit(src, spec.run_dir)
                draft.manual = True
                ui.notify(f"копия {src.rel} в каталоге прогона: правьте её в редакторе")
                dialog.close()
                await rebuild()
                _config_dialog(ctx, draft, rebuild)  # заново, уже с редактором копии

            ui.button("Править в этом прогоне", on_click=edit).props("flat no-caps dense")


# --- карточка прогона ----------------------------------------------------------


def _run_dialog(ctx: AppContext, run_id: str, after) -> None:
    try:
        run = ctx.benches.get(run_id)
    except BenchError as exc:
        ui.notify(str(exc), type="negative")
        return
    preview = Preview.from_json(run.preview_json)
    secrets = list(ctx.benches.secrets().values())

    def refresh() -> None:
        dialog.close()
        after()
        _run_dialog(ctx, run_id, after)

    async def do(coro) -> None:
        try:
            await coro
        except BenchError as exc:
            ui.notify(str(exc), type="negative", multi_line=True)
            return
        refresh()

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label(run.id).classes("text-lg break-all")
        facts = [
            _status_text(run),
            f"ревизия {run.ref}@{run.sha[:8]}" if run.ref and run.ref != run.sha else f"ревизия {run.sha[:8]}",
            f"version_id {run.version_id if run.version_id is not None else '—'}" + (f" (сессия {run.session_id})" if run.session_id else ""),
            f"начат {_when(run.started_at)}",
            f"завершён {_when(run.finished_at)}" if run.finished_at else _duration(run),
            f"requeue {run.requeues}",
        ]
        ui.label(" · ".join(facts)).classes("text-sm")
        if run.error:
            ui.label(run.error).classes("text-negative text-sm whitespace-pre-wrap")
        if run.status == "attention":
            ui.label("Модель поднята, GPU тратятся: повторите стадию или погасите." if run.holds_gpu else "Задачи могут идти: повторите стадию или остановите прогон.").classes(
                "text-negative text-sm"
            )
        c = run.counters
        if c:
            ui.label(
                f"задачи: идут {c.get('running', 0)}, ждут {c.get('pending_wf', 0)} · очередь сабмиттера {c.get('queue', '—')}, отправлено {c.get('submitted', '—')}, "
                f"завершено {c.get('completed', '—')} · сабмиттер {'жив' if c.get('submitter_alive') else 'не запущен'} · реплик живых {c.get('replicas', '—')} · "
                f"лимит агентов {run.agent_limit if run.agent_limit is not None else '—'} · на {_when(c.get('at'))}"
            ).classes("text-sm")
        rows = [{"k": k, "v": v} for k, v in preview.rows()] if run.preview_json != "{}" else []
        rows += [{"k": "Идентификаторы", "v": f"run_id {run.id} · лейбл Argo fleet/run-name={run.id}" + (f" · MLSC version_id {run.version_id}" if run.version_id is not None else "")}]
        with ui.expansion("Что и где запущено", value=run.active).classes("w-full"):
            _facts_table(rows)
        base = ctx.settings.argo_ui_url
        with ui.row().classes("gap-3 flex-wrap text-sm"):
            ui.link("Argo UI по прогону", argo_ui_url(base, ctx.settings.namespace, run.id), new_tab=True)
            if preview.s3:
                ui.label(f"S3: {preview.s3}").classes("opacity-70 break-all")
            ui.label(f"каталог: {run.dir}").classes("opacity-70 break-all")
        with ui.row().classes("items-end gap-2 flex-wrap"):
            if run.status == "running":
                ui.button("Остановить прогон", on_click=lambda: _stop_dialog(ctx, run, refresh)).props("flat no-caps color=negative")
            if run.status in ("attention", "failed", "stopped") and run.stage:
                ui.button(f"Повторить стадию {run.stage}", on_click=lambda: do(ctx.benches.retry(run.id))).props("unelevated no-caps")
            if run.status == "attention":
                ui.button("Погасить и остановить", on_click=lambda: _stop_dialog(ctx, run, refresh)).props("flat no-caps color=negative")
            if not run.active and run.status != "done":
                ui.button("Замер по тому, что есть", on_click=lambda: do(ctx.benches.measure(run.id))).props("flat no-caps")
            if run.status == "done":
                ui.button("Замер заново", on_click=lambda: do(ctx.benches.measure(run.id))).props("flat no-caps")
            if run.active:
                ui.label("лимит и реплики — в карточке прогона на странице «Кластер»").classes("text-xs opacity-60")
        ui.label("Стадии").classes("mt-2")
        stages = ctx.benches.stages(run.id)
        if not stages:
            ui.label("ещё не начинались").classes("text-sm opacity-70")
        for st in stages:
            result = "идёт" if st.returncode is None and st.finished_at is None else ("прервана" if st.returncode is None else f"код {st.returncode}")
            title = f"{STAGE_TITLES.get(st.stage, st.stage)} · {result} · {_when(st.started_at)}"
            with ui.expansion(title).classes("w-full").props("dense"):
                ui.code(st.command, language="bash").classes("w-full")
                if st.log_path:
                    ui.button("Хвост лога", on_click=lambda p=st.log_path: _log_tail_dialog(p, secrets)).props("flat dense no-caps")
        events = [e for e in ctx.store.events(300) if e.object == run.id][:15]
        if events:
            with ui.expansion(f"Журнал ({len(events)})").classes("w-full").props("dense"):
                ui.label("\n".join(f"{_when(e.ts)} {e.action} {e.result} {e.details}" for e in events)).classes("font-mono text-xs whitespace-pre-wrap")
        ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
    dialog.open()


def _duration(run: BenchRun) -> str:
    if not run.started_at:
        return "не начат"
    minutes = int((datetime.now(timezone.utc) - datetime.fromisoformat(run.started_at)).total_seconds() // 60)
    return f"идёт {minutes // 60} ч {minutes % 60} мин" if minutes >= 60 else f"идёт {minutes} мин"


def _stop_dialog(ctx: AppContext, run: BenchRun, after) -> None:
    c = run.counters
    lines = [
        f"задач идёт {c.get('running', 0)}, в очереди {c.get('queue', '—')}: лимит агентов станет 0, сабмиттер прогона будет убит, активным workflows уйдёт Terminate",
        f"модель: версия {run.version_id} будет погашена (mlsc.stop_llm)" if run.holds_gpu else ("модель сессии не гасится" if run.session_id else "модели нет — гасить нечего"),
        "стадия, если идёт, будет убита",
    ]
    confirm_dialog(f"Остановить прогон {run.id}?", lines, lambda: ctx.benches.stop(run.id), after, button="Остановить")


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

