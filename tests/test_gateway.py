import socket

import httpx
import pytest

import fake_gateway
from fleet_console.connect.gateway import Gateway, free_port, list_models, parse_target, probe, snippets


def test_parse_target_and_argv() -> None:
    assert parse_target("litellm-argo/litellm-fleet:80") == ("litellm-argo", "litellm-fleet", 80)
    assert parse_target("vllm-proxy/nginx-vllm-proxy-test") == ("vllm-proxy", "nginx-vllm-proxy-test", 80)
    with pytest.raises(ValueError):
        parse_target("litellm-fleet")
    gw = Gateway("litellm", "litellm-argo", "litellm-fleet", 80, 18080)
    assert gw.argv == ["kubectl", "-n", "litellm-argo", "port-forward", "service/litellm-fleet", "18080:80"]
    assert gw.base_url == "http://127.0.0.1:18080/v1" and gw.name == "gateway-litellm"


def test_free_port_falls_back_when_busy() -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert free_port(port) != port
    assert free_port(port) == port  # освободился — предпочтение соблюдено


async def test_list_models_and_probe() -> None:
    http = fake_gateway.client()
    models = await list_models(http, "http://127.0.0.1:1/v1")
    assert [m.id for m in models] == ["GLM-5.3-Flash", "Qwen3"] and models[0].max_input_tokens == 524288
    assert await probe(http, "http://127.0.0.1:1/v1")
    assert await probe(http, "http://127.0.0.1:1/other")  # 404 от nginx — шлюз ответил
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused"))))
    assert not await probe(down, "http://127.0.0.1:1/v1")


def test_snippets_mention_url_and_model() -> None:
    s = snippets("http://127.0.0.1:18080/v1", "GLM-5.3-Flash")
    assert s["curl"].startswith("curl http://127.0.0.1:18080/v1/chat/completions") and '"model": "GLM-5.3-Flash"' in s["curl"]
    assert 'base_url="http://127.0.0.1:18080/v1"' in s["python"] and 'model="GLM-5.3-Flash"' in s["python"]
