"""Страница «Ревизия»: ветка → коммит → «Подготовить ревизию» → каталог с шапками."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

from fleet_console.catalog import GROUP_TITLES, group_order
from fleet_console.context import AppContext
from fleet_console.fleetref.manager import PrepareError
from fleet_console.ui.header import build_header

PHASES = {
    "clone": "клонирую зеркало fleet",
    "fetch": "git fetch --prune",
    "worktree": "готовлю worktree",
    "sync": "uv sync --frozen (первый раз ~30 с, нужен бастион)",
    "profile": "собираю профиль ревизии: fleet --help, --print-config",
}


def _title(ref: str, sha: str) -> str:
    """«master @ 378874ba», а для ссылки-SHA — просто «378874ba»."""
    short = sha[:8]
    if not ref or ref == sha or sha.startswith(ref) or ref.startswith(short):
        return short
    return f"{ref} @ {short}"


async def revision_page(ctx: AppContext) -> None:
    build_header(ctx)
    state = {"ref": "", "info": ""}

    @ui.refreshable
    def info_view() -> None:
        if state["info"]:
            ui.label(state["info"]).classes("text-sm whitespace-pre-wrap")

    @ui.refreshable
    def progress_view() -> None:
        p = ctx.manager.progress
        if p.busy:
            ui.linear_progress(show_value=False).props("indeterminate").classes("w-full")
            what = _title(p.ref, p.sha) if p.sha else p.ref
            ui.label((what + ": " if what else "") + PHASES.get(p.phase, p.phase)).classes("text-sm")
            tail = list(p.log)[-8:]
            if tail:
                ui.label("\n".join(tail)).classes("whitespace-pre-wrap font-mono text-xs opacity-80 w-full")
        elif p.phase == "error":
            ui.label(p.error).classes("text-negative whitespace-pre-wrap")

    @ui.refreshable
    def result_view() -> None:
        r = ctx.manager.current
        if r is None:
            return
        pr = r.profile
        with ui.column().classes("w-full gap-2"):
            ui.label(f"Готово: {_title(r.ref, r.info.sha)}" + (" (из кеша)" if r.from_cache else "")).classes("text-lg")
            facts = [
                f"Harbor {pr.harbor_version or '?'}",
                f"образ раннера {pr.agent_image or '?'}",
                "--print-config есть" if pr.print_config else "--print-config нет",
                f"шагов {len(pr.steps)}",
                f"раскладка {pr.layout}",
            ]
            ui.label(" · ".join(facts)).classes("text-sm opacity-80")
            if pr.missing:
                title = "Предупреждения:" if pr.compatible else "Ревизия несовместима с пресетами:"
                ui.label(title).classes("text-warning" if pr.compatible else "text-negative")
                for reason in pr.missing:
                    ui.label("• " + reason).classes("text-sm whitespace-pre-wrap")
            with ui.expansion("Шаги fleet в этой ревизии", caption=f"{len(pr.steps)} секций с _target_").classes("w-full"):
                ui.label("\n".join(pr.steps) or "нет").classes("font-mono text-sm whitespace-pre-wrap")
            _catalog(ctx, r.worktree)

    async def show_info() -> None:
        ref = state["ref"]
        if not ref:
            state["info"] = ""
        else:
            try:
                info = await ctx.manager.commit_info(ref)
            except Exception as exc:  # noqa: BLE001 — неизвестный SHA/тег: показываем текст
                state["info"] = f"Не нашёл «{ref}» в зеркале: {exc}"
                info_view.refresh()
                return
            merged = "влит в master" if info.merged_into_main else f"не влит, отстаёт от master на {info.behind_main}"
            cached = " · профиль в кеше" if ctx.manager.is_ready(info.sha) else ""
            state["info"] = f"{info.short} · {info.author} · {info.date:%d.%m.%Y %H:%M} · {merged}{cached}\n{info.subject}"
        info_view.refresh()

    async def on_branch(branch: str | None) -> None:
        if not branch:
            return
        commits = await ctx.manager.commits(branch)
        options = {c.sha: f"{c.short} · {c.date:%d.%m %H:%M} · {c.subject[:70]}" for c in commits}
        commit_select.set_options(options, value=commits[0].sha if commits else None)

    async def on_commit(ref: str | None) -> None:
        state["ref"] = ref or ""
        await show_info()

    async def prepare() -> None:
        if not state["ref"]:
            ui.notify("Выберите ветку и коммит или введите SHA")
            return
        if ctx.manager.progress.busy:
            ui.notify("Подождите: операция уже идёт")
            return
        prepare_button.disable()
        try:
            await ctx.manager.prepare(state["ref"])
        except PrepareError:
            pass  # текст в progress.error
        finally:
            prepare_button.enable()
        progress_view.refresh()
        result_view.refresh()
        await show_info()

    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
        ui.label("Ревизия fleet").classes("text-xl")
        with ui.row().classes("w-full items-end gap-3 flex-wrap"):
            branch_select = ui.select({}, label="Ветка", with_input=True, on_change=lambda e: on_branch(e.value))
            branch_select.classes("flex-1 min-w-[16rem]")
            commit_select = ui.select({}, label="Коммит", with_input=True, on_change=lambda e: on_commit(e.value))
            commit_select.classes("flex-1 min-w-[16rem]")
            ref_input = ui.input("или SHA / тег", on_change=lambda e: on_commit(e.value.strip() or commit_select.value))
            ref_input.classes("min-w-[12rem]")
            prepare_button = ui.button("Подготовить ревизию", on_click=prepare).props("unelevated no-caps")
        mirror_note = ui.label("Зеркало fleet ещё не склонировано — ветки появятся сами, когда клонирование закончится.")
        mirror_note.classes("text-sm opacity-80").set_visibility(not ctx.manager.mirror.exists())
        info_view()
        progress_view()
        result_view()

    async def load_branches() -> None:
        branches = await ctx.manager.branches()
        names = [b.name for b in branches]
        options = {b.name: f"{b.name} · {b.date:%d.%m} · {b.author}" for b in branches}
        branch_select.set_options(options, value="master" if "master" in names else (names[0] if names else None))
        mirror_note.set_visibility(False)
        state["loaded"] = True

    async def tick() -> None:
        progress_view.refresh()
        if not state.get("loaded") and ctx.manager.mirror.exists() and not ctx.manager.progress.busy:
            await load_branches()  # зеркало появилось после открытия страницы

    if ctx.manager.mirror.exists():
        await load_branches()
    ui.timer(1.0, tick)


def _catalog(ctx: AppContext, worktree: Path) -> None:
    entries = ctx.catalog(worktree)
    if not entries:
        return
    ui.label("Каталог конфигов ревизии").classes("text-lg mt-2")
    groups: dict[str, list] = {}
    for e in entries:
        groups.setdefault(e.group, []).append(e)
    for group, _ in sorted(group_order(list(groups)), key=lambda x: x[1]):
        items = groups[group]
        with ui.expansion(GROUP_TITLES.get(group, group), caption=f"{len(items)} файл(ов)").classes("w-full"):
            for e in items:
                with ui.expansion(e.name, caption=e.summary).classes("w-full").props("dense"):
                    ui.label(e.header or "без шапки-комментария").classes("whitespace-pre-wrap font-mono text-sm")
                    ui.label(e.path + (f" · @package {e.package}" if e.package else "")).classes("text-xs opacity-70 break-all")
