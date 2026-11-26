from copy import deepcopy

import pytest
from test_benches import NS, Harness

from fleet_console.compose import SessionLink, from_resolved
from fleet_console.compose.model_route import model_route_problem
from fleet_console.runner import FakeRunner
from fleet_console.runs import BenchError


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def config(inference="litellm", base="http://route.test/custom/v1/"):
    return {
        "experiment": {"model_name": "owner/model", "inference": inference},
        "mlsc": {"served_model_name": "owner/model"},
        "harbor_job": {"agents": [{"model_name": "openai/owner/model"}, {"model_name": "owner/model"}]},
        "system": {"polar": {"topology": {"gateway": {"nodes": [{"inference": {"base_url": base}}]}}}},
    }


def session_spec(h):
    return h.spec(session=SessionLink("session", "owner/model", "glm", 1))


async def test_resolved_agent_override_blocks_submission(h):
    with pytest.raises(BenchError) as exc:
        await h.start(extra=("harbor_job.agents.0.model_name=openai/Other",))
    assert "harbor_job.agents[0].model_name='openai/Other'" in str(exc.value)
    assert h.spec().effective_model in str(exc.value)
    assert not h.store.get_bench(h.spec().run_id)


async def test_all_effective_agents_and_session_name(h):
    spec = session_spec(h)
    data = config()
    runner = FakeRunner()

    async def check():
        return await model_route_problem(spec, data, from_resolved(data), runner, h.settings, session_ready=False)

    assert not await check()
    data["system"]["materialize"] = {"job_source": {"config": deepcopy(data["harbor_job"])}}
    agent = data["system"]["materialize"]["job_source"]["config"]["agents"][1]
    agent["model_name"] = "openai/openai/owner/model"
    detail = await check()
    assert "agents[1]" in detail and agent["model_name"] in detail and "модель сессии='owner/model'" in detail
    agent["model_name"] = "owner/model"
    data["mlsc"]["served_model_name"] = "someone-else"
    assert "mlsc.served_model_name='someone-else'" in await check()
    data["experiment"]["model_name"] = "someone-else"
    assert "модель сессии='owner/model'" in await check()
    assert runner.calls == []


@pytest.mark.parametrize(("body", "code", "detail"), [
    ('{"data":[{"id":"owner/model"}]}', 0, ""),
    ('{"data":[{"id":"owner/model-other"},{"id":"OWNER/model"}]}', 0, "отсутствует точный ID"),
    ('{"data":[]}', 0, "отсутствует точный ID"),
    ("", 1, "Не удалось прочитать"),
    ("{}", 0, "Не удалось прочитать"),
])
async def test_ready_litellm_exact_id_at_resolved_gateway(h, body, code, detail):
    data = config()
    runner = FakeRunner()
    runner.on("kubectl", "exec", "-n", NS, h.settings.cluster.submitter_pod, "--", "curl", stdout=body, returncode=code)
    result = await model_route_problem(session_spec(h), data, from_resolved(data), runner, h.settings, session_ready=True)
    assert (detail in result) if detail else (result == "")
    assert [argv[-1] for argv, _ in runner.calls] == ["http://route.test/custom/v1/models"]


@pytest.mark.parametrize(("body", "code", "detail"), [
    ("upstream owner/model {\n server host:80;\n}", 0, ""),
    ("upstream owner/model-other {\n server host:80;\n}", 0, "отсутствует точный ID"),
    ("upstream owner/model {}", 0, "нет реплик"),
    ("", 1, "Не удалось прочитать"),
])
async def test_ready_nginx_uses_upstream(h, body, code, detail):
    data = config("nginx", "http://nginx-vllm-proxy-test.vllm-proxy.svc.cluster.local")
    runner = FakeRunner()
    runner.on("kubectl", "-n", "vllm-proxy", "get", "configmap", h.settings.cluster.nginx_configmap, stdout=body, returncode=code)
    result = await model_route_problem(session_spec(h), data, from_resolved(data), runner, h.settings, session_ready=True)
    assert (detail in result) if detail else (result == "")
    assert len(runner.calls) == 1
    data = config("nginx", "http://litellm.litellm-argo")
    result = await model_route_problem(session_spec(h), data, from_resolved(data), runner, h.settings, session_ready=True)
    assert "Для nginx ожидается" in result and len(runner.calls) == 1


async def test_new_model_needs_only_config(h):
    data = config()
    runner = FakeRunner()
    spec = h.spec(isolate_model=False, model_name="owner/model")
    assert not await model_route_problem(spec, data, from_resolved(data), runner, h.settings, session_ready=False)
    assert not runner.calls
