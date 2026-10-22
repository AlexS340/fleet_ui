"""Фейковый OpenAI-совместимый шлюз для httpx.MockTransport: /v1/models, /v1/chat/completions и /health/readiness litellm."""

import json

import httpx

MODELS = {
    "data": [
        {"id": "GLM-5.3-Flash", "object": "model", "max_input_tokens": 524288},
        {"id": "Qwen3", "object": "model"},
    ],
    "object": "list",
}


def sse(chunks: list[dict]) -> str:
    return "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


STREAM = sse(
    [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "думаю"}}]},
        {"choices": [{"index": 0, "delta": {"content": "Привет"}}]},
        {"choices": [{"index": 0, "delta": {"content": ", мир"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "ls", "arguments": '{"path":'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "/"}'}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 7}},
    ]
)
COMPLETION = {
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Ответ целиком", "reasoning_content": "кратко"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
}


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/health/readiness":
        return httpx.Response(200, json={"status": "healthy", "db": "connected"})
    if request.url.path.endswith("/v1/models"):
        return httpx.Response(200, json=MODELS)
    if request.url.path.endswith("/v1/chat/completions"):
        body = json.loads(request.content)
        if body.get("model") == "broken":
            return httpx.Response(500, text="boom")
        if body.get("stream"):
            return httpx.Response(200, text=STREAM, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=COMPLETION)
    return httpx.Response(404, text="model not found or model not set in request")


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))
