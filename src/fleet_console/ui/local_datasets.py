"""Локальные задачи на странице прогонов: папка, проверка, снимок, S3."""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

import yaml
from nicegui import ui

from fleet_console.compose.local_datasets import LocalDataset, validate_folder
from fleet_console.context import AppContext
from fleet_console.fleetref.profile import CONFIGS_REL


def folder_picker(on_select) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
        ui.label("Выберите папку на компьютере сервиса").classes("text-lg")
        path = ui.input("Текущая папка", value=str(Path.home())).classes("w-full")
        error = ui.label().classes("text-negative text-sm")
        table = ui.table(columns=[{"name": "name", "field": "name", "label": "Папки", "align": "left"}], rows=[], row_key="path").classes("w-full").props("dense flat")

        def browse(target=None) -> None:
            try:
                current = Path(target or path.value).expanduser().resolve(strict=True)
                rows = [{"name": "↑ На уровень выше", "path": str(current.parent)}]
                rows += [{"name": p.name, "path": str(p)} for p in sorted(current.iterdir()) if p.is_dir() and not p.name.startswith(".")]
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


def import_dialog(ctx: AppContext, after) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
        ui.label("Добавить локальные задачи").classes("text-lg")
        ui.label("Выберите одну задачу или папку, непосредственно содержащую задачи. Для набора по языкам выберите en или ru.").classes("text-sm opacity-70")
        folder = ui.input("Папка с задачами").classes("w-full").mark("local-folder")
        ui.button("Выбрать папку…", on_click=lambda: folder_picker(selected)).props("flat no-caps")
        name = ui.input("Название набора").classes("w-full").mark("local-name")
        result = ui.label().classes("w-full text-sm whitespace-pre-wrap break-words").mark("local-validation")
        ui.label("Проверяем структуру и TOML без исполнения файлов. Совместимость с выбранным Harbor дополнительно проверяется перед выделением GPU.").classes("text-xs opacity-60")
        with ui.row().classes("gap-2 flex-wrap"):
            check = ui.button("Проверить папку").props("flat no-caps").mark("local-check")
            save = ui.button("Добавить набор").props("unelevated no-caps").mark("local-import")
            save.disable()
            ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")

        async def validate() -> None:
            save.disable()
            check.disable()
            checked_path = folder.value or ""
            try:
                report = await asyncio.to_thread(validate_folder, Path(checked_path))
                if folder.value != checked_path:
                    return
                result.set_text(f"Задач: {len(report.tasks)} · {report.size / 1024 / 1024:.1f} МБ\n" +
                                "\n".join(report.errors[:15] + report.warnings[:5]) +
                                ("\nМинимальная проверка пройдена." if report.ok else "\nИсправьте ошибки перед добавлением."))
                if report.ok:
                    save.enable()
                    if not name.value:
                        name.set_value(Path(folder.value).name)
            finally:
                check.enable()

        async def selected(value: str) -> None:
            folder.set_value(value)
            await validate()

        async def add() -> None:
            save.disable()
            try:
                item = await asyncio.to_thread(ctx.local_datasets.import_folder, Path(folder.value), name.value or "")
                ui.notify(f"Добавлен набор «{item.name}»: {len(item.tasks)} задач")
                dialog.close()
                after()
            except (OSError, ValueError) as exc:
                result.set_text(str(exc))

        check.on_click(validate)
        save.on_click(add)
        folder.on_value_change(lambda _: save.disable())
    dialog.open()


def default_s3_root(ctx: AppContext) -> str:
    revision = ctx.manager.current
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


def dataset_dialog(ctx: AppContext, item: LocalDataset, on_run) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-3xl"):
        ui.label(item.name).classes("text-lg")
        ui.label(f"Из локальной папки · задач: {len(item.tasks)} · {item.size / 1024 / 1024:.1f} МБ").classes("text-sm")
        ui.label(f"Источник: {item.source}").classes("w-full text-sm break-all")
        ui.label(f"Сохранённая копия: {item.path}").classes("w-full text-xs opacity-60 break-all")
        saved_root = item.s3_uri.split(f"/local/{item.id}/tasks/")[0] if item.s3_uri else default_s3_root(ctx)
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
            ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")

        def refresh() -> None:
            current = ctx.local_datasets.get(item.id)
            active = item.id in ctx.local_datasets.jobs and not ctx.local_datasets.jobs[item.id].done()
            try:
                command.set_content(shlex.join(ctx.local_datasets.upload_argv(current, root.value or "")))
                valid = True
            except ValueError as exc:
                command.set_content(str(exc))
                valid = False
            state.set_text("Отправляется в S3…" if active else current.error or (f"Загружено в S3: {current.s3_uri}" if current.uploaded_at else "Пока только локальная копия. Сначала отправьте задачи в S3."))
            upload.set_enabled(valid and not active)
            run.set_enabled(bool(current.uploaded_at) and not active)
            root.set_enabled(not active and not current.s3_uri)
            log = ctx.paths.logs / "datasets" / f"{item.id}.upload.log"
            log_box.set_visibility(log.is_file())
            if log.is_file():
                log_text.set_content(log.read_text(encoding="utf-8")[-20_000:])

        async def send() -> None:
            try:
                job = ctx.local_datasets.upload(item.id, root.value or "")
            except (OSError, ValueError) as exc:
                state.set_text(str(exc))
                return
            refresh()
            await job
            refresh()

        def start() -> None:
            dialog.close()
            on_run(ctx.local_datasets.get(item.id))

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
        table.on("rowClick", lambda e: dataset_dialog(ctx, ctx.local_datasets.get(e.args[1]["id"]), on_run))

        def refresh() -> None:
            rows = []
            for item in ctx.local_datasets.all():
                active = item.id in ctx.local_datasets.jobs and not ctx.local_datasets.jobs[item.id].done()
                status = "Отправляется в S3" if active else "Ошибка загрузки" if item.error else "В S3 · можно запускать" if item.uploaded_at else "Локальная копия · нужно отправить в S3"
                rows.append({"id": item.id, "name": item.name, "tasks": len(item.tasks), "status": status})
            table.update_rows(rows)
            table.set_visibility(bool(rows))

        refresh()
        ui.timer(2, refresh)
