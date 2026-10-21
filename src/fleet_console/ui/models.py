"""Страница «Модели»: проброс к шлюзу моделей и песочница запросов. Сессии моделей — следующий шаг Ф2."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from nicegui import ui

from fleet_console.connect.gateway import Gateway, list_models, snippets
from fleet_console.context import AppContext
from fleet_console.models import SessionError, sandbox
from fleet_console.models.compose import recipe_summary
from fleet_console.store import Session
from fleet_console.ui.common import confirm_dialog
from fleet_console.ui.header import STATUS_COLORS, STATUS_WORDS, build_header

GATEWAY_OPTIONS = {"litellm": "litellm — маршрут fleet по умолчанию", "nginx": "nginx — vllm-proxy"}
SESSION_COLUMNS = [
    {"name": "id", "label": "Сессия", "field": "id", "align": "left", "sortable": True},
    {"name": "model", "label": "Модель", "field": "model", "align": "left"},
    {"name": "status", "label": "Статус", "field": "status", "align": "left"},
    {"name": "replicas", "label": "Реплики (план/факт)", "field": "replicas", "align": "right"},
    {"name": "version", "label": "version_id", "field": "version", "align": "right"},
    {"name": "expires", "label": "Истекает", "field": "expires", "align": "left"},
]
STATUS_TEXT = {
    "starting": "поднимается",
    "ready": "готова",
    "stopping": "гасится",
    "stopped": "погашена",
    "failed": "не удалась",
    "attention": "требует внимания",
}


async def models_page(ctx: AppContext) -> None:
    build_header(ctx)
    state: dict = {"gateway": None, "models": [], "on_models": None}
    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
        ui.label("Модели").classes("text-xl")
        _sessions_card(ctx)
        _gateway_card(ctx, state)
        _sandbox_card(ctx, state)


# --- сессии моделей ------------------------------------------------------
def _status_text(s: Session) -> str:
    text = STATUS_TEXT.get(s.status, s.status)
    return f"{text}: {s.stage}" if s.status == "starting" and s.stage else text


def _expires_text(s: Session) -> str:
    if s.status != "ready" or not s.expires_at:
        return "—"
    minutes = int((datetime.fromisoformat(s.expires_at) - datetime.now(timezone.utc)).total_seconds() // 60)
    return f"через {minutes} мин" if minutes >= 0 else f"истекла {-minutes} мин назад"


def _when_iso(text: str | None) -> str:
    return datetime.fromisoformat(text).astimezone().strftime("%d.%m %H:%M") if text else "—"


def _sessions_card(ctx: AppContext) -> None:
    """Главное действие страницы — поднять модель; таблица сессий и карточка по клику."""
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center gap-3"):
            ui.label("Сессии моделей").classes("text-lg")
            ui.space()
            ui.button("Поднять модель", on_click=lambda: _new_session_dialog(ctx, render)).props("unelevated no-caps")
        table = ui.table(columns=SESSION_COLUMNS, rows=[], row_key="id").classes("w-full").props("dense flat")
        table.on("rowClick", lambda e: _session_dialog(ctx, e.args[1]["id"], render))
        empty = ui.label("Сессий нет. «Поднять модель» создаёт версию в MLSC шагами fleet и гасит её по таймеру.").classes(
            "text-sm opacity-70"
        )
        ui.label("клик по строке — карточка сессии: стадии и их команды, логи, продление, реплики, гашение").classes("text-xs opacity-60")

    def render() -> None:
        versions = {v.version_id: v for v in ctx.observer.mlsc.data or []}
        rows = []
        for s in ctx.sessions.sessions()[:20]:
            v = versions.get(s.version_id) if s.version_id is not None else None
            rows.append(
                {
                    "id": s.id,
                    "model": s.model or s.recipe,
                    "status": _status_text(s),
                    "replicas": f"{s.replicas} ({v.planned}/{v.current})" if v else str(s.replicas),
                    "version": s.version_id if s.version_id is not None else "—",
                    "expires": _expires_text(s),
                }
            )
        table.update_rows(rows)
        table.set_visibility(bool(rows))
        empty.set_visibility(not rows)

    render()
    ui.timer(3.0, render)


def _new_session_dialog(ctx: AppContext, after) -> None:
    revision = ctx.manager.current
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-xl"):
        ui.label("Поднять модель").classes("text-lg")
        if revision is None or not revision.profile.compatible:
            ui.label("Сначала подготовьте совместимую ревизию fleet на главной странице: сессия идёт её шагами mlsc.*").classes("text-sm")
            ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
            dialog.open()
            return
        ref = revision.ref if revision.ref != revision.info.sha else revision.info.sha[:8]
        ui.label(f"ревизия {ref}@{revision.info.sha[:8]}" if ref != revision.info.sha[:8] else f"ревизия {ref}").classes("text-sm opacity-70")
        recipes = revision.profile.groups.get("mlsc/model", [])
        recipe = ui.select(recipes, value=recipes[0] if recipes else None, label="Рецепт mlsc/model").classes("w-full")
        info = ui.label().classes("text-xs opacity-70")
        model = ui.input("served_model_name (пусто — из рецепта; суффикс спасает от коллизий имён)").classes("w-full")
        with ui.row().classes("gap-3 flex-wrap"):
            replicas = ui.number("Реплики", value=1, min=1, precision=0).classes("w-28")
            ttl = ui.number("Время жизни, мин", value=ctx.settings.session_ttl_minutes, min=5, precision=0).classes("w-40")
            inference = ui.select({"litellm": "litellm", "nginx": "nginx"}, value="litellm", label="Маршрут").classes("w-32")
        with ui.expansion("Дополнительно").classes("w-full"):
            extra = ui.textarea("overrides fleet, по одному на строку: ключ=значение").classes("w-full").props("autogrow")
        gpu_label = ui.label().classes("text-sm")
        summary: dict = {}

        def refresh_recipe() -> None:
            summary.clear()
            summary.update(recipe_summary(revision.worktree, recipe.value) if recipe.value else {})
            info.set_text(
                f"в рецепте: served_model_name {summary.get('served_model_name', '?')}, инстанс {summary.get('instance_type_name', '?')}"
            )
            gpu = summary.get("gpu_per_instance")
            n = int(replicas.value or 1)
            gpu_label.set_text(f"GPU: {n} × {gpu} = {n * gpu}" if gpu else "GPU на инстанс в рецепте не указан — смотрите resolved.yaml после preflight")

        recipe.on_value_change(lambda _: refresh_recipe())
        replicas.on_value_change(lambda _: refresh_recipe())
        refresh_recipe()

        def ask() -> None:
            n = int(replicas.value or 1)
            gpu = summary.get("gpu_per_instance")
            name = (model.value or "").strip() or summary.get("served_model_name", "(из рецепта)")
            lines = [
                f"рецепт {recipe.value}, модель {name}, маршрут {inference.value}",
                f"реплик {n}" + (f" × {gpu} GPU = {n * gpu} GPU" if gpu else ""),
                f"время жизни {int(ttl.value or 0)} мин, затем уведомление и гашение через {ctx.settings.session_grace_minutes} мин",
                "стадии: preflight → mlsc.create_job → mlsc.start_llm → mlsc.check_llm_works; команды видны в карточке",
            ]

            async def start() -> None:
                session = await ctx.sessions.start(
                    revision,
                    recipe=recipe.value,
                    model_name=(model.value or "").strip(),
                    replicas=n,
                    inference=inference.value,
                    ttl_minutes=int(ttl.value or 0) or None,
                    extra=tuple(line.strip() for line in (extra.value or "").splitlines() if line.strip()),
                )
                dialog.close()
                ui.notify(f"Сессия {session.id}: подъём начат")

            confirm_dialog("Занять GPU под модель?", lines, start, after, button="Да, поднять")

        with ui.row().classes("gap-2"):
            ui.button("Запустить подъём", on_click=ask).props("unelevated no-caps")
            ui.button("Отмена", on_click=dialog.close).props("flat no-caps")
    dialog.open()


def _session_dialog(ctx: AppContext, session_id: str, after) -> None:
    s = ctx.sessions.get(session_id)
    versions = {v.version_id: v for v in ctx.observer.mlsc.data or []}
    v = versions.get(s.version_id) if s.version_id is not None else None

    def refresh() -> None:
        dialog.close()
        after()
        _session_dialog(ctx, session_id, after)

    async def do(coro) -> None:
        try:
            await coro
        except SessionError as exc:
            ui.notify(str(exc), type="negative", multi_line=True)
            return
        refresh()

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-3xl"):
        ui.label(s.id).classes("text-lg break-all")
        facts = [
            _status_text(s),
            f"рецепт {s.recipe}",
            f"модель {s.model or '(из рецепта)'}",
            f"ревизия {s.ref}@{s.sha[:8]}",
            f"маршрут {s.inference}",
            f"реплик {s.replicas}" + (f" — в MLSC план {v.planned}, факт {v.current}, {v.gpus or 0} GPU" if v else ""),
            f"version_id {s.version_id if s.version_id is not None else '—'}",
            f"создана {_when_iso(s.created_at)}",
            f"истекает {_expires_text(s)}",
        ]
        ui.label(" · ".join(facts)).classes("text-sm")
        if s.error:
            ui.label(s.error).classes("text-negative text-sm whitespace-pre-wrap")
        ui.label(f"каталог: {s.dir}").classes("text-xs opacity-60 break-all")
        if s.active:
            with ui.row().classes("items-end gap-2 flex-wrap"):
                if s.status in ("ready", "attention"):
                    ui.button("Продлить +60 мин", on_click=lambda: do(ctx.sessions.extend(s.id, 60))).props("flat no-caps")
                    replicas = ui.number("Реплики", value=s.replicas, min=0, precision=0).classes("w-28")

                    def ask_replicas() -> None:
                        n = int(replicas.value or 0)
                        confirm_dialog(
                            f"Сессии выставить {n} реплик",
                            [f"сейчас {s.replicas}; mlsc.start_llm с experiment.jobs={n}", "0 — то же, что погасить"],
                            lambda: ctx.sessions.set_replicas(s.id, n),
                            refresh,
                        )

                    ui.button("Применить", on_click=ask_replicas).props("flat no-caps")
                label = "Прервать подъём" if s.status == "starting" else "Погасить"
                detail = (
                    f"mlsc.stop_llm: версии {s.version_id} будет выставлено 0 инстансов"
                    if s.version_id is not None
                    else "версии MLSC ещё нет — сессия просто отметится погашенной"
                )
                ui.button(
                    label,
                    on_click=lambda: confirm_dialog(f"{label}: {s.id}", [detail], lambda: ctx.sessions.stop(s.id), refresh, button=label),
                ).props("flat no-caps color=negative")
        ui.label("Стадии").classes("mt-2")
        stages = ctx.sessions.stages(s.id)
        if not stages:
            ui.label("ещё не начинались").classes("text-sm opacity-70")
        for st in stages:
            result = "идёт" if st.returncode is None else f"код {st.returncode}"
            with ui.expansion(f"{st.stage} · {result} · {_when_iso(st.started_at)}").classes("w-full"):
                ui.code(st.command, language="bash").classes("w-full")
                if st.log_path:
                    ui.button("Хвост лога", on_click=lambda p=st.log_path: _log_tail_dialog(p)).props("flat dense no-caps")
        ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
    dialog.open()


def _log_tail_dialog(path: str, lines: int = 80) -> None:
    try:
        text = "\n".join(Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]) or "(пусто)"
    except OSError as exc:
        text = f"лог не прочитать: {exc}"
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label(path).classes("text-xs opacity-60 break-all")
        ui.label(text).classes("font-mono text-xs whitespace-pre-wrap w-full")
        ui.button("Закрыть", on_click=dialog.close).props("flat no-caps")
    dialog.open()


# --- шлюз и песочница ----------------------------------------------------


def _gateway_card(ctx: AppContext, state: dict) -> None:
    """Проброс порта к шлюзу под супервизором, список моделей, сниппеты."""
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            ui.label("Шлюз моделей").classes("text-lg")
            key = ui.select(GATEWAY_OPTIONS, value=next(iter(ctx.gateways), "litellm"), label="Шлюз").classes("w-80")
            toggle = ui.button("Пробросить порт", on_click=lambda: _toggle()).props("unelevated no-caps")
            status = ui.label().classes("text-sm")
        with ui.row().classes("items-center gap-2 flex-wrap") as url_row:
            ui.label("base URL:").classes("text-sm opacity-70")
            url = ui.label().classes("font-mono")
            ui.button("Скопировать", on_click=lambda: ui.clipboard.write(url.text)).props("flat dense no-caps")
            ui.button("Обновить модели", on_click=lambda: refresh_models()).props("flat dense no-caps")
        models_label = ui.label().classes("text-sm whitespace-pre-wrap")
        with ui.expansion("Сниппеты").classes("w-full") as snippets_box:
            curl = ui.code("", language="bash").classes("w-full")
            python = ui.code("", language="python").classes("w-full")
        ui.label("Проброс живёт, пока его не закрыть; в шапке он виден отдельным чипом.").classes("text-xs opacity-60")

    def gateway() -> Gateway | None:
        return ctx.gateways.get(key.value)

    async def _toggle() -> None:
        if gateway():
            ctx.close_gateway(key.value)
            ui.notify(f"Проброс к {key.value} закрыт")
        else:
            gw = ctx.open_gateway(key.value)
            ui.notify(f"Проброс к {gw.key}: {gw.base_url}")
        render()
        await refresh_models()

    async def refresh_models() -> None:
        gw = gateway()
        if gw is None:
            state["models"] = []
            models_label.set_text("")
        elif gw.key == "nginx":
            state["models"] = sorted(ctx.observer.nginx.data or {})
            models_label.set_text("модели из ConfigMap upstream nginx: " + (", ".join(state["models"]) or "пока ни одной"))
        else:
            try:
                models = await list_models(ctx.http, gw.base_url)
            except Exception as exc:  # noqa: BLE001 — шлюз ещё не проброшен или не отвечает: показываем причину
                state["models"] = []
                models_label.set_text(f"GET {gw.base_url}/models: {exc}")
            else:
                state["models"] = [m.id for m in models]
                names = [f"{m.id} ({m.max_input_tokens // 1024}k)" if m.max_input_tokens else m.id for m in models]
                models_label.set_text("модели в шлюзе: " + (", ".join(names) or "ни одной"))
        _snippets()
        if state["on_models"]:
            state["on_models"]()

    def _snippets() -> None:
        gw = gateway()
        model = state["models"][0] if state["models"] else "<model>"
        s = snippets(gw.base_url, model) if gw else {"curl": "", "python": ""}
        curl.set_content(s["curl"])
        python.set_content(s["python"])

    def render() -> None:
        gw = gateway()
        state["gateway"] = gw
        toggle.set_text("Закрыть проброс" if gw else "Пробросить порт")
        url_row.set_visibility(gw is not None)
        snippets_box.set_visibility(gw is not None)
        if gw is None:
            status.set_text("")
            return
        st = ctx.supervisor.state(gw.name)
        status.set_text(f"{STATUS_WORDS[st.status]}{' — ' + st.note if st.note else ''}")
        status.classes(replace=f"text-sm text-{STATUS_COLORS[st.status]}")
        url.set_text(gw.base_url)

    async def on_key_change(_) -> None:
        render()
        await refresh_models()

    async def tick() -> None:
        render()
        gw = state["gateway"]
        if gw and not state["models"] and ctx.supervisor.state(gw.name).status == "green":
            await refresh_models()  # проброс только что поднялся — подтянуть список

    key.on_value_change(on_key_change)
    render()
    ui.timer(3.0, tick)


def _sandbox_card(ctx: AppContext, state: dict) -> None:
    """Диалог с моделью через проброшенный шлюз: текст, reasoning, tool_calls, тайминги."""
    messages: list[dict] = []
    with ui.card().classes("w-full"):
        ui.label("Песочница").classes("text-lg")
        with ui.row().classes("w-full gap-3 flex-wrap items-end"):
            model = ui.select([], label="Модель", with_input=True).classes("w-80")
            temperature = ui.number("temperature", value=0.7, min=0, max=2, step=0.1).classes("w-28")
            max_tokens = ui.number("max_tokens", value=1024, min=1, precision=0).classes("w-28")
            stream = ui.switch("потоком", value=True)
        system = ui.textarea("Системный промпт").classes("w-full").props("autogrow")
        with ui.expansion("Дополнительно").classes("w-full"):
            tools = ui.textarea("tools — JSON-список инструментов").classes("w-full").props("autogrow")
        history = ui.column().classes("w-full gap-2")
        message = ui.textarea("Сообщение").classes("w-full").props("autogrow")
        with ui.row().classes("gap-2"):
            send = ui.button("Отправить", on_click=lambda: _send()).props("unelevated no-caps")
            ui.button("Очистить", on_click=lambda: _clear()).props("flat no-caps")

    def on_models() -> None:
        options = state["models"]
        value = model.value if model.value in options else (options[0] if options else None)
        model.set_options(options, value=value)

    state["on_models"] = on_models

    def _clear() -> None:
        messages.clear()
        history.clear()

    def _bubble(role: str, text: str) -> None:
        with history, ui.card().classes("w-full"):
            ui.label(role).classes("text-xs opacity-60")
            ui.label(text).classes("whitespace-pre-wrap")

    async def _send() -> None:
        gw = state["gateway"]
        text = (message.value or "").strip()
        if gw is None:
            ui.notify("Сначала пробросьте порт к шлюзу", type="warning")
            return
        if not model.value:
            ui.notify("Выберите модель", type="warning")
            return
        if not text:
            return
        try:
            tools_list = json.loads(tools.value) if (tools.value or "").strip() else None
        except ValueError as exc:
            ui.notify(f"tools: не JSON — {exc}", type="negative")
            return
        messages.append({"role": "user", "content": text})
        message.set_value("")
        _bubble("user", text)
        body = sandbox.build_body(
            model.value, system.value or "", messages,
            temperature=float(temperature.value or 0), max_tokens=int(max_tokens.value or 1), tools=tools_list, stream=bool(stream.value),
        )
        with history, ui.card().classes("w-full"):
            ui.label(f"assistant · {model.value}").classes("text-xs opacity-60")
            out = ui.label("…").classes("whitespace-pre-wrap")
            with ui.expansion("reasoning").classes("w-full") as reasoning_box:
                reasoning = ui.label().classes("whitespace-pre-wrap text-sm opacity-80")
            reasoning_box.set_visibility(False)
            calls = ui.code("", language="json").classes("w-full")
            calls.set_visibility(False)
            metrics = ui.label().classes("text-xs opacity-70")
        send.disable()
        try:
            result = await sandbox.chat(ctx.http, gw.base_url, body, on_delta=lambda r: out.set_text(r.text or "…"))
        finally:
            send.enable()
        if result.error:
            out.set_text(result.error)
            out.classes("text-negative")
            messages.pop()  # вопрос не дошёл — пусть повтор не дублирует его в истории
            return
        out.set_text(result.text or "(пустой ответ)")
        if result.reasoning:
            reasoning.set_text(result.reasoning)
            reasoning_box.set_visibility(True)
        if result.tool_calls:
            calls.set_content(json.dumps(result.tool_calls, ensure_ascii=False, indent=1))
            calls.set_visibility(True)
        metrics.set_text(sandbox.describe(result))
        messages.append(result.as_message())
