"""Страница «Модели»: проброс к шлюзу моделей и песочница запросов. Сессии моделей — следующий шаг Ф2."""

from __future__ import annotations

import json

from nicegui import ui

from fleet_console.connect.gateway import Gateway, list_models, snippets
from fleet_console.context import AppContext
from fleet_console.models import sandbox
from fleet_console.ui.header import STATUS_COLORS, STATUS_WORDS, build_header

GATEWAY_OPTIONS = {"litellm": "litellm — маршрут fleet по умолчанию", "nginx": "nginx — vllm-proxy"}


async def models_page(ctx: AppContext) -> None:
    build_header(ctx)
    state: dict = {"gateway": None, "models": [], "on_models": None}
    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
        ui.label("Модели").classes("text-xl")
        _gateway_card(ctx, state)
        _sandbox_card(ctx, state)
        ui.label("Сессии моделей — подъём и гашение через MLSC — следующий шаг Ф2.").classes("text-sm opacity-60")


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
