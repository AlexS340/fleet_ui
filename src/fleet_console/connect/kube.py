"""Kube context, доступность API-сервера и права — через kubectl."""

from __future__ import annotations

from dataclasses import dataclass, field
import json

from fleet_console.runner import Runner

Check = tuple[str, str, str | None]  # verb, resource, namespace


def required_checks(namespace: str, argo_namespace: str) -> list[Check]:
    """Права, без которых сервис не сможет работать (Ф0–Ф2)."""
    return [
        ("list", "workflows.argoproj.io", namespace),
        ("patch", "workflows.argoproj.io", namespace),
        ("create", "pods/exec", namespace),
        ("create", "pods/exec", "vllm-proxy"),
        ("get", "configmaps", "vllm-proxy"),
        ("create", "pods/portforward", argo_namespace),
        ("get", "nodes", None),
    ]


@dataclass
class KubeStatus:
    context: str | None = None
    server: str | None = None
    allowed: bool = False
    ready: bool = False
    error: str = ""
    permissions: dict[str, bool] = field(default_factory=dict)

    @property
    def missing_permissions(self) -> list[str]:
        return [k for k, ok in self.permissions.items() if not ok]


class Kube:
    def __init__(self, runner: Runner, allowed_contexts: list[str]) -> None:
        self.runner = runner
        self.allowed_contexts = allowed_contexts

    async def current_context(self) -> str | None:
        result = await self.runner.run(["kubectl", "config", "current-context"], timeout=15)
        return result.stdout.strip() if result.ok else None

    async def server_url(self) -> str | None:
        result = await self.runner.run(["kubectl", "config", "view", "--minify", "-o", "json"], timeout=15)
        if not result.ok:
            return None
        try:
            clusters = json.loads(result.stdout).get("clusters") or []
            return clusters[0]["cluster"]["server"]
        except (ValueError, KeyError, IndexError, TypeError):
            return None

    async def readyz(self) -> bool:
        result = await self.runner.run(["kubectl", "get", "--raw", "/readyz"], timeout=10)
        return result.ok and result.stdout.strip() == "ok"

    async def can_i(self, checks: list[Check]) -> dict[str, bool]:
        answers: dict[str, bool] = {}
        for verb, resource, namespace in checks:
            argv = ["kubectl", "auth", "can-i", verb, resource]
            key = f"{verb} {resource}"
            if namespace:
                argv += ["-n", namespace]
                key += f" -n {namespace}"
            result = await self.runner.run(argv, timeout=15)
            answers[key] = result.ok and result.stdout.strip() == "yes"
        return answers

    async def status(self, checks: list[Check] | None = None) -> KubeStatus:
        st = KubeStatus()
        st.context = await self.current_context()
        if st.context is None:
            st.error = "kubectl не вернул текущий context: проверьте kubeconfig"
            return st
        st.server = await self.server_url()
        st.allowed = st.context in self.allowed_contexts
        if not st.allowed:
            st.error = f"context «{st.context}» не в списке разрешённых {self.allowed_contexts}"
            return st
        st.ready = await self.readyz()
        if not st.ready:
            st.error = f"API-сервер {st.server or ''} не отвечает на /readyz"
            return st
        if checks:
            st.permissions = await self.can_i(checks)
            if st.missing_permissions:
                st.error = "нет прав: " + ", ".join(st.missing_permissions)
        return st
