"""Песочница: один запрос chat/completions к проброшенному шлюзу, потоком или целиком.

Собирает текст, reasoning (если модель его отдаёт), tool_calls, время до первого токена,
общее время и usage. Формат — OpenAI-совместимый, как у vLLM за litellm и nginx.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from time import perf_counter
from typing import Any

import httpx

TIMEOUT = httpx.Timeout(600.0, connect=10.0)


@dataclass
class ChatResult:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    ttft_ms: float | None = None  # до первого токена (контента или reasoning)
    total_ms: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str = ""
    error: str = ""

    def as_message(self) -> dict[str, Any]:
        """Сообщение ассистента для продолжения диалога."""
        message: dict[str, Any] = {"role": "assistant", "content": self.text}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        return message


def build_body(
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.7,
    max_tokens: int = 1024,
    tools: list[dict[str, Any]] | None = None,
    stream: bool = True,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system.strip() else []) + list(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if tools:
        body["tools"] = tools
    if stream:
        body["stream_options"] = {"include_usage": True}
    return body


def describe(result: ChatResult) -> str:
    parts = []
    if result.ttft_ms is not None:
        parts.append(f"до первого токена {result.ttft_ms:.0f} мс")
    parts.append(f"всего {result.total_ms / 1000:.1f} с")
    if result.prompt_tokens is not None or result.completion_tokens is not None:
        parts.append(f"токены {result.prompt_tokens if result.prompt_tokens is not None else '?'} → "
                     f"{result.completion_tokens if result.completion_tokens is not None else '?'}")
    if result.finish_reason:
        parts.append(f"finish: {result.finish_reason}")
    return " · ".join(parts)


async def chat(http: httpx.AsyncClient, base_url: str, body: dict[str, Any], on_delta=None) -> ChatResult:
    """Выполнить запрос; on_delta(result) зовётся на каждом фрагменте потока."""
    result = ChatResult()
    url = f"{base_url}/chat/completions"
    started = perf_counter()
    try:
        if body.get("stream"):
            async with http.stream("POST", url, json=body, timeout=TIMEOUT) as response:
                if response.status_code != 200:
                    text = (await response.aread()).decode(errors="replace")
                    result.error = f"HTTP {response.status_code}: {text[:500]}"
                else:
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        _apply_chunk(result, json.loads(payload), started)
                        if on_delta:
                            on_delta(result)
        else:
            response = await http.post(url, json=body, timeout=TIMEOUT)
            if response.status_code != 200:
                result.error = f"HTTP {response.status_code}: {response.text[:500]}"
            else:
                _apply_completion(result, response.json(), started)
    except (httpx.HTTPError, ValueError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    result.total_ms = (perf_counter() - started) * 1000
    return result


def _apply_chunk(result: ChatResult, chunk: dict[str, Any], started: float) -> None:
    _apply_usage(result, chunk.get("usage"))
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or {}
        content = delta.get("content") or ""
        reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
        if (content or reasoning) and result.ttft_ms is None:
            result.ttft_ms = (perf_counter() - started) * 1000
        result.text += content
        result.reasoning += reasoning
        for call in delta.get("tool_calls") or []:
            _merge_tool_call(result.tool_calls, call)
        if choice.get("finish_reason"):
            result.finish_reason = choice["finish_reason"]


def _apply_completion(result: ChatResult, data: dict[str, Any], started: float) -> None:
    _apply_usage(result, data.get("usage"))
    choices = data.get("choices") or []
    if not choices:
        return
    choice = choices[0]
    message = choice.get("message") or {}
    result.text = message.get("content") or ""
    result.reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    result.tool_calls = list(message.get("tool_calls") or [])
    result.finish_reason = choice.get("finish_reason") or ""
    result.ttft_ms = (perf_counter() - started) * 1000


def _apply_usage(result: ChatResult, usage: dict[str, Any] | None) -> None:
    if usage:
        result.prompt_tokens = usage.get("prompt_tokens", result.prompt_tokens)
        result.completion_tokens = usage.get("completion_tokens", result.completion_tokens)


def _merge_tool_call(calls: list[dict[str, Any]], delta: dict[str, Any]) -> None:
    """Потоковые tool_calls приходят кусками по index: имя один раз, аргументы — конкатенацией."""
    index = delta.get("index", len(calls))
    while len(calls) <= index:
        calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
    call = calls[index]
    call["id"] = delta.get("id") or call["id"]
    call["type"] = delta.get("type") or call["type"]
    function = delta.get("function") or {}
    call["function"]["name"] = function.get("name") or call["function"]["name"]
    call["function"]["arguments"] += function.get("arguments") or ""
