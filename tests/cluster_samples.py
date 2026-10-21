"""Образцы ответов kubectl для тестов слоя observe и UI-страницы «Кластер»."""

import json

ARGO_TEXT = (
    "wf-a-1\tRunning\trun-a\tGLM\t2026-10-21T05:00:00Z\t\n"
    "wf-a-2\tSucceeded\trun-a\tGLM\t2026-10-21T04:00:00Z\t2026-10-21T04:30:00Z\n"
    "wf-a-3\t\trun-a\tGLM\t2026-10-21T05:01:00Z\t\n"
    "wf-b-1\tFailed\trun-b\tQwen\t2026-10-20T10:00:00Z\t2026-10-20T11:00:00Z\n"
    "wf-b-2\tError\trun-b\tQwen\t2026-10-20T10:00:00Z\t2026-10-20T11:30:00Z\n"
    "wf-x\tSucceeded\t\t\t2026-10-19T10:00:00Z\t2026-10-19T10:10:00Z\n"
)

CONFIGURATION = json.dumps(
    {
        "application_version_states": {"41": {"planned": 4, "current": 3}, "40": {"planned": 0, "current": 0}, "42": {"planned": 0, "current": 1}},
        "application_versions": {
            "41": {
                "application_name": "gigacode-fleet-egbulychev run-a",
                "job_command_line": "serve -- vllm serve /models/glm --served-model-name GLM-5.3-Flash",
                "instance_type_name": "a100plus.8gpu.80vG.96C.1456G",
                "queue_name": "gigacode",
            },
            "42": {"application_name": "someone-else app", "job_env_vars": {"MODEL_NAME": "Qwen3"}, "instance_type_name": "a100.4gpu"},
        },
    }
)

NGINX_CONF = """
upstream GLM-5.3-Flash {
    server 10.0.0.1:8000;
    server 10.0.0.2:8000;
}
upstream Qwen3 {
    server 10.0.0.9:8000;
}
"""

SUBMITTER_TEXT = "run-a\t40\t12\t30\t5\t1\t1761022800\nrun-b\t\t0\t0\t90\t0\t1760950800\n"

NODES_TEXT = (
    "bench-1\t\tTrue\t95500m\t1456Gi\n"
    "bench-2\t\tTrue\t96\t1500000000Ki\n"
    "bench-3\ttrue\tFalse\t96\t1456Gi\n"
)


def install(fake, namespace: str = "swe-bench-v2-20260308") -> None:
    """Ответы FakeRunner для всех источников дашборда."""
    fake.on("kubectl", "-n", namespace, "get", "workflows", stdout=ARGO_TEXT)
    fake.on("kubectl", "-n", "vllm-proxy", "get", "deploy", stdout='{"app":"litellm-noauth"}')
    fake.on("kubectl", "-n", "vllm-proxy", "get", "pods", stdout="litellm-noauth-old\tTerminating\nlitellm-noauth-abc\tRunning\n")
    fake.on("kubectl", "exec", "-i", "-n", "vllm-proxy", stdout=CONFIGURATION)
    fake.on("kubectl", "-n", "vllm-proxy", "get", "configmap", stdout=NGINX_CONF)
    fake.on("kubectl", "exec", "-n", namespace, "fleet-submitter-python", "--", "sh", stdout=SUBMITTER_TEXT)
    fake.on("kubectl", "exec", "-n", namespace, "fleet-submitter-python", "--", "tail", stdout="[1/3] submitted wf-a-1\n")
    fake.on("kubectl", "get", "nodes", stdout=NODES_TEXT)
    fake.on("kubectl", "-n", namespace, "get", "pods", stdout="pod/x\npod/y\n")
