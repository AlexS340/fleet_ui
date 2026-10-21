import fake_gateway
from fleet_console.models import sandbox

URL = "http://127.0.0.1:1/v1"


def test_build_body() -> None:
    body = sandbox.build_body("m", "будь краток", [{"role": "user", "content": "привет"}], tools=[{"type": "function"}], stream=True)
    assert body["messages"][0] == {"role": "system", "content": "будь краток"} and body["messages"][1]["content"] == "привет"
    assert body["tools"] == [{"type": "function"}] and body["stream_options"] == {"include_usage": True}
    plain = sandbox.build_body("m", "  ", [], stream=False)
    assert plain["messages"] == [] and "stream_options" not in plain and "tools" not in plain


async def test_stream_collects_text_reasoning_tool_calls_and_usage() -> None:
    seen: list[str] = []
    result = await sandbox.chat(fake_gateway.client(), URL, sandbox.build_body("m", "", [], stream=True), on_delta=lambda r: seen.append(r.text))
    assert result.error == "" and result.text == "Привет, мир" and result.reasoning == "думаю"
    assert result.tool_calls == [{"id": "call_1", "type": "function", "function": {"name": "ls", "arguments": '{"path": "/"}'}}]
    assert (result.prompt_tokens, result.completion_tokens, result.finish_reason) == (12, 7, "tool_calls")
    assert result.ttft_ms is not None and result.total_ms >= result.ttft_ms
    assert seen[-1] == "Привет, мир" and "" in seen  # первый фрагмент был только reasoning
    assert result.as_message()["tool_calls"][0]["function"]["name"] == "ls"
    assert "до первого токена" in sandbox.describe(result) and "токены 12 → 7" in sandbox.describe(result)


async def test_plain_completion_and_http_error() -> None:
    result = await sandbox.chat(fake_gateway.client(), URL, sandbox.build_body("m", "", [], stream=False))
    assert (result.text, result.reasoning, result.finish_reason, result.prompt_tokens) == ("Ответ целиком", "кратко", "stop", 10)
    broken = await sandbox.chat(fake_gateway.client(), URL, sandbox.build_body("broken", "", [], stream=True))
    assert broken.error.startswith("HTTP 500") and "boom" in broken.error
    broken2 = await sandbox.chat(fake_gateway.client(), URL, sandbox.build_body("broken", "", [], stream=False))
    assert broken2.error.startswith("HTTP 500")
