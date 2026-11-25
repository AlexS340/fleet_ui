"""Локальные задачи на странице прогонов: папка, проверка, снимок, S3."""

from __future__ import annotations

import asyncio
import shlex
from hashlib import sha256
from pathlib import Path

import yaml
from nicegui import ui

from fleet_console.auth import AccessDenied
from fleet_console.compose.local_datasets import ImportValidationError, LocalDataset, Validation, validate_import
from fleet_console.context import AppContext
from fleet_console.fleetref.profile import CONFIGS_REL
from fleet_console.ui.common import confirm_dialog


def folder_picker(on_select, root: Path | None = None) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
        ui.label("Выберите папку на компьютере сервиса").classes("text-lg")
        path = ui.input("Текущая папка", value=str(root or Path.home())).classes("w-full")
        error = ui.label().classes("text-negative text-sm")
        table = ui.table(columns=[{"name": "name", "field": "name", "label": "Папки", "align": "left"}], rows=[], row_key="path").classes("w-full").props("dense flat")

        def browse(target=None) -> None:
            try:
                current = _import_path(target or path.value, root)
                rows = [{"name": "↑ На уровень выше", "path": str(current.parent)}] if current != root else []
                rows += [{"name": p.name, "path": str(p)} for p in sorted(current.iterdir())
                         if p.is_dir() and not p.name.startswith(".") and (root is None or not p.is_symlink())]
                path.set_value(str(current))
                table.update_rows(rows)
                error.set_text("")
            except (OSError, ValueError) as exc:
                error.set_text(str(exc))

        table.on("rowClick", lambda e: browse(e.args[1]["path"]))
        with ui.row().classes("w-full gap-2 flex-wrap"):
            ui.button("Открыть путь", on_click=lambda: browse()).props("flat no-caps")
            ui.space()
            ui.button("Отмена", on_click=dialog.close).props("flat no-caps")

            async def choose() -> None:
                dialog.close()
                await on_select(path.value)

            ui.button("Выбрать эту папку", on_click=choose).props("unelevated no-caps")
        browse()
    dialog.open()


def _import_path(value: str, root: Path | None) -> Path:
    path = Path(value).expanduser()
    if root is not None and not path.is_absolute():
        path = root / path
    path = path.resolve()
    if root is not None and not path.is_relative_to(root):
        raise ValueError("Выберите папку в своём каталоге импорта")
    return path


def import_dialog(ctx: AppContext, after) -> None:
    owner = ctx.access.id
    root = None if ctx.access.admin else ctx.paths.home.resolve() / "imports" / sha256(owner.encode()).hexdigest()
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
        ui.label("Добавить локальные задачи").classes("text-lg")
        ui.label("Выберите одну задачу или папку, непосредственно содержащую задачи. Для набора по языкам выберите en или ru.").classes("text-sm opacity-70")
        folder = ui.input("Папка с задачами", value=str(root) if root else "").classes("w-full").mark("local-folder")
        ui.button("Выбрать папку…", on_click=lambda: folder_picker(selected, root)).props("flat no-caps")
        name = ui.input("Название набора").classes("w-full").mark("local-name")
        result = ui.label().classes("w-full text-sm whitespace-pre-wrap break-words").mark("local-validation")
        issues = ui.column().classes("w-full gap-2 overflow-y-auto").style("max-height: 30vh")
        issues.set_visibility(False)
        ui.label("Проверяем файлы задач и основные образы во внутреннем реестре. Совместимость с Harbor проверяется перед выделением GPU.").classes("text-xs opacity-60")
        with ui.row().classes("gap-2 flex-wrap"):
            check = ui.button("Проверить папку").props("flat no-caps").mark("local-check")
            save = ui.button("Добавить набор").props("unelevated no-caps").mark("local-import")
            save.disable()
            ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")

        def show_report(report: Validation) -> None:
            lines = [f"Задач: {len(report.tasks)} · {report.size / 1024 / 1024:.1f} МБ",
                     *report.errors[:15], *report.warnings[:5]]
            lines.append("Минимальная проверка пройдена." if report.ok else "Исправьте ошибки перед добавлением.")
            result.set_text("\n".join(lines))
            issues.clear()
            issues.set_visibility(bool(report.image_issues))
            with issues:
                for image, issue in report.image_issues.items():
                    with ui.column().classes("w-full gap-0 text-sm"):
                        ui.label(issue.message).classes("font-medium")
                        ui.label(", ".join(report.images[image])).classes("w-full break-words")
                        if issue.url:
                            ui.link(image, issue.url, new_tab=True).classes("w-full break-all")
                        else:
                            ui.label(image).classes("w-full break-all")

        def reset_result() -> None:
            save.disable()
            result.set_text("")
            issues.set_visibility(False)

        async def validate() -> None:
            reset_result()
            result.set_text("Проверяем папку и образы…")
            check.disable()
            checked_path = folder.value or ""
            try:
                report = await asyncio.to_thread(validate_import, _import_path(checked_path, root), allow_task_links=root is None)
                if folder.value != checked_path:
                    return
                show_report(report)
                if report.ok:
                    save.enable()
                    if not name.value:
                        name.set_value(Path(folder.value).name)
            except (OSError, ValueError) as exc:
                result.set_text(str(exc))
            finally:
                check.enable()

        async def selected(value: str) -> None:
            folder.set_value(value)
            await validate()

        async def add() -> None:
            save.disable()
            try:
                item = await asyncio.to_thread(
                    ctx.local_datasets.import_folder, _import_path(folder.value, root), name.value or "",
                    owner_id=owner, allow_task_links=root is None,
                )
                ui.notify(f"Добавлен набор «{item.name}»: {len(item.tasks)} задач")
                dialog.close()
                after()
            except ImportValidationError as exc:
                show_report(exc.report)
            except (OSError, ValueError) as exc:
                result.set_text(str(exc))

        check.on_click(validate)
        save.on_click(add)
        folder.on_value_change(lambda _: reset_result())
    dialog.open()


def default_s3_root(ctx: AppContext) -> str:
    revision = ctx.revision
    if revision is None:
        return ""
    try:
        data = yaml.safe_load((revision.worktree / CONFIGS_REL / "system/cluster.yaml").read_text()) or {}
        root = str(data.get("s3_datasets_root") or "${.bucket}/${.namespace}/harbor_datasets")
        for key in ("bucket", "namespace"):
            root = root.replace("${." + key + "}", str(data.get(key) or "")).replace("${system." + key + "}", str(data.get(key) or ""))
        return root if root.startswith("s3://") and "${" not in root else ""
    except (OSError, yaml.YAMLError):
        return ""


def dataset_dialog(ctx: AppContext, item: LocalDataset, on_run, after) -> None:
    item = ctx.user_dataset(item.id)
    default_root = default_s3_root(ctx)
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-3xl"):
        ui.label(item.name).classes("text-lg")
        summary = ui.label().classes("text-sm")
        with ui.expansion("Состав набора").classes("w-full"):
            selected = ui.select(item.tasks, label="Задача", with_input=True).classes("w-full").mark("local-task")
            remove_task = ui.button("Удалить задачу", on_click=lambda: ask_remove_task()).props("flat no-caps color=negative")
            remove_task.set_visibility(item.owner_id == ctx.access.id)
            last_task = ui.label("В наборе должна остаться хотя бы одна задача.").classes("text-xs opacity-70")
        ui.label(f"Источник: {item.source}").classes("w-full text-sm break-all")
        ui.label(f"Сохранённая копия: {item.path}").classes("w-full text-xs opacity-60 break-all")
        saved_root = item.s3_root or default_root
        root = ui.input("S3-префикс датасетов", value=saved_root).classes("w-full")
        ui.label(f"Профиль записи: {ctx.settings.s3_write_profile} · отправка создаёт отдельную папку этого набора в S3").classes("text-xs opacity-70")
        with ui.expansion("Команда загрузки").classes("w-full"):
            command = ui.code("", language="shell").classes("w-full whitespace-pre-wrap break-all")
        state = ui.label().classes("w-full text-sm whitespace-pre-wrap break-all").mark("local-upload-state")
        with ui.expansion("Лог отправки").classes("w-full") as log_box:
            log_text = ui.code("", language="text").classes("w-full").style("max-height: 18rem; overflow: auto")
        with ui.row().classes("gap-2 flex-wrap"):
            upload = ui.button("Отправить в S3").props("unelevated no-caps").mark("local-upload")
            run = ui.button("Запустить на этих данных").props("flat no-caps").mark("local-run")
            delete = ui.button("Удалить набор", on_click=lambda: ask_delete()).props("flat no-caps color=negative")
            delete.set_visibility(item.owner_id == ctx.access.id)
            ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")

        def ask_delete() -> None:
            if reason := ctx.local_datasets.deletion_blocker(ctx.user_dataset(item.id)):
                ui.notify(reason, type="warning")
                return

            async def remove() -> None:
                ctx.user_dataset(item.id)
                ctx.local_datasets.delete(item.id, owner_id=ctx.access.id)
                timer.deactivate()
                dialog.close()

            confirm_dialog("Удалить набор?", [
                f"Console удалит запись импорта набора «{item.name}», маппинг и локальную копию задач.",
                "Исходная папка и файлы в S3 сохранятся.",
            ], remove, after, button="Да, удалить набор")

        def ask_remove_task() -> None:
            name = selected.value
            if not name:
                return

            async def remove() -> None:
                ctx.user_dataset(item.id)
                await ctx.local_datasets.remove_task(item.id, name, owner_id=ctx.access.id)
                refresh()
                after()

            confirm_dialog(f"Удалить задачу «{name}» из набора «{item.name}»?", [
                "Исходная папка и ранее загруженные файлы в S3 сохранятся.",
                "Перед следующим запуском отправьте обновлённый набор в S3.",
            ], remove, button="Да, удалить задачу")

        def refresh() -> None:
            try:
                current = ctx.user_dataset(item.id)
            except AccessDenied:  # карточка может быть открыта в другой вкладке во время удаления
                timer.deactivate()
                dialog.close()
                after()
                return
            active = item.id in ctx.local_datasets.jobs and not ctx.local_datasets.jobs[item.id].done()
            summary.set_text(f"Из локальной папки · задач: {len(current.tasks)} · {current.size / 1024 / 1024:.1f} МБ")
            if selected.options != current.tasks:
                selected.set_options(current.tasks, value=selected.value if selected.value in current.tasks else None)
            remove_task.set_enabled(bool(selected.value) and len(current.tasks) > 1 and not active)
            last_task.set_visibility(len(current.tasks) == 1 and item.owner_id == ctx.access.id)
            try:
                command.set_content(shlex.join(ctx.local_datasets.upload_argv(current, (root.value or "") if ctx.access.admin else default_root)))
                valid = True
            except ValueError as exc:
                command.set_content(str(exc))
                valid = False
            state.set_text("Отправляется в S3…" if active else current.error or (f"Загружено в S3: {current.s3_uri}" if current.uploaded_at else "Пока только локальная копия. Сначала отправьте задачи в S3."))
            upload.set_enabled(valid and not active)
            delete.set_enabled(not active)
            run.set_enabled(bool(current.uploaded_at) and not active)
            root.set_enabled(ctx.access.admin and not active and not current.s3_uri)
            log = ctx.paths.logs / "datasets" / f"{item.id}.upload.log"
            log_box.set_visibility(log.is_file())
            if log.is_file():
                log_text.set_content(log.read_text(encoding="utf-8")[-20_000:])

        async def send() -> None:
            try:
                ctx.user_dataset(item.id)
                job = ctx.local_datasets.upload(item.id, (root.value or "") if ctx.access.admin else default_root)
            except (OSError, ValueError) as exc:
                state.set_text(str(exc))
                return
            refresh()
            await job
            refresh()

        def start() -> None:
            dialog.close()
            on_run(ctx.user_dataset(item.id))

        selected.on_value_change(lambda _: refresh())
        upload.on_click(send)
        run.on_click(start)
        root.on_value_change(lambda _: refresh())
        timer = ui.timer(1, refresh)
        dialog.on("hide", timer.deactivate)
        refresh()
    dialog.open()


def local_panel(ctx: AppContext, on_run) -> None:
    with ui.expansion("Локальные задачи", value=True).classes("w-full"):
        ui.label("Добавьте задачи с компьютера, отправьте в S3 и выберите их для прогона.").classes("text-sm opacity-70")
        ui.button("Добавить из папки", on_click=lambda: import_dialog(ctx, refresh)).props("flat no-caps").mark("local-add")
        ui.label("Клик по набору — отправить в S3 или перейти к запуску.").classes("text-xs opacity-60")
        table = ui.table(columns=[{"name": k, "field": k, "label": label, "align": "left"} for k, label in
                                  (("name", "Набор"), ("tasks", "Задач"), ("status", "Состояние"))], rows=[], row_key="id").classes("w-full").props("dense flat wrap-cells").mark("local-datasets")
        table.on("rowClick", lambda e: dataset_dialog(ctx, ctx.user_dataset(e.args[1]["id"]), on_run, refresh))

        def refresh() -> None:
            rows = []
            for item in ctx.user_datasets():
                active = item.id in ctx.local_datasets.jobs and not ctx.local_datasets.jobs[item.id].done()
                status = "Отправляется в S3" if active else "Ошибка загрузки" if item.error else "В S3 · можно запускать" if item.uploaded_at else "Локальная копия · нужно отправить в S3"
                rows.append({"id": item.id, "name": item.name, "tasks": len(item.tasks), "status": status})
            table.update_rows(rows)
            table.set_visibility(bool(rows))

        refresh()
        ui.timer(2, refresh)
