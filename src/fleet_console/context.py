"""Сборка сервиса: настройки, журнал, kube, зеркало, супервизор, менеджер ревизий."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
import shlex

import httpx

from fleet_console.catalog import CatalogEntry, scan
from fleet_console.connect.gateway import Gateway, free_port, parse_target, probe
from fleet_console.connect.kube import Kube, KubeStatus, required_checks
from fleet_console.connect.supervisor import Forward, Supervisor, subprocess_spawner
from fleet_console.fleetref.git import Mirror
from fleet_console.fleetref.manager import PrepareError, RevisionManager
from fleet_console.observe import ClusterObserver
from fleet_console.runner import SubprocessRunner
from fleet_console.settings import Paths, Settings, default_home, read_secret
from fleet_console.store import Store

BASTION = "bastion"
KUBE_POLL_SEC = 30.0
KUBE_RETRY_SEC = 5.0  # пока API-сервер не ответил (ssh ещё поднимается) — опрашиваем чаще


@dataclass
class AppContext:
    settings: Settings
    paths: Paths
    store: Store
    kube: Kube
    manager: RevisionManager
    supervisor: Supervisor
    observer: ClusterObserver
    kube_status: KubeStatus = field(default_factory=KubeStatus)
    http: httpx.AsyncClient = field(default_factory=httpx.AsyncClient)  # шлюз моделей и песочница
    gateways: dict[str, Gateway] = field(default_factory=dict)  # открытые пробросы к шлюзам по ключу
    port_picker: Callable[[int], int] = free_port
    _tasks: list[asyncio.Task] = field(default_factory=list)

    def catalog(self, worktree: Path) -> list[CatalogEntry]:
        return scan(worktree)

    # --- шлюз моделей ----------------------------------------------------
    def open_gateway(self, key: str) -> Gateway:
        """Поднять port-forward к шлюзу под супервизором; повторный вызов возвращает уже открытый."""
        if key in self.gateways:
            return self.gateways[key]
        c = self.settings.cluster
        namespace, service, port = parse_target({"litellm": c.litellm_gateway, "nginx": c.nginx_gateway}[key])
        gateway = Gateway(key, namespace, service, port, self.port_picker(c.gateway_ports.get(key, 0)))

        async def health() -> bool:
            return await probe(self.http, gateway.base_url)

        depends_on = BASTION if self.settings.bastion.ssh_host else None
        self.supervisor.add(
            Forward(gateway.name, gateway.title, gateway.argv, health, depends_on=depends_on, hint=f"Руками: {shlex.join(gateway.argv)}")
        )
        self.gateways[key] = gateway
        self.store.event("open_gateway", key, "ok", gateway.base_url)
        return gateway

    def close_gateway(self, key: str) -> None:
        gateway = self.gateways.pop(key, None)
        if gateway is None:
            return
        self.supervisor.remove(gateway.name)
        self.store.event("close_gateway", key, "ok", gateway.base_url)

    def needs_permission_check(self) -> bool:
        """Права проверяем, пока их не знаем или пока хоть одно «✗»: отказ бывает транзитным (ssh только поднялся)."""
        return not self.kube_status.permissions or bool(self.kube_status.missing_permissions)

    async def refresh_kube(self, with_permissions: bool = False) -> None:
        previous = self.kube_status
        checks = required_checks(self.settings.namespace) if with_permissions else None
        status = await self.kube.status(checks)
        if not with_permissions and previous.permissions:
            status.permissions = previous.permissions  # права не зависят от связи: показываем последние известные
            if status.missing_permissions and not status.error:
                status.error = "нет прав: " + ", ".join(status.missing_permissions)
        self.kube_status = status

    async def startup(self) -> None:
        self.observer.ready = lambda: self.kube_status.ready  # без API-сервера kubectl только плодит ошибки
        self._tasks = [
            asyncio.create_task(self.supervisor.run(), name="supervisor"),
            asyncio.create_task(self._kube_loop(), name="kube"),
            asyncio.create_task(self._mirror_init(), name="mirror"),
            asyncio.create_task(self.observer.loop(), name="observer"),
        ]
        if self.settings.auto_fetch_minutes > 0:
            self._tasks.append(asyncio.create_task(self._fetch_loop(), name="auto-fetch"))

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.supervisor.stop()
        await self.http.aclose()
        self.store.close()

    async def _kube_loop(self) -> None:
        await self.refresh_kube(with_permissions=True)
        while True:
            await asyncio.sleep(KUBE_POLL_SEC if self.kube_status.ready else KUBE_RETRY_SEC)
            await self.refresh_kube(with_permissions=self.needs_permission_check())

    async def _mirror_init(self) -> None:
        try:
            await self.manager.ensure_mirror()
            await self.manager.gc_worktrees()
        except PrepareError:
            pass  # текст ошибки уже в manager.progress.error, UI его покажет

    async def _fetch_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.auto_fetch_minutes * 60)
            if self.manager.mirror.exists() and not self.manager.progress.busy:
                try:
                    await self.manager.fetch()
                except PrepareError:
                    pass


def build_context(home: Path | None = None) -> AppContext:
    paths = Paths(home or default_home()).ensure()
    settings = Settings.load(paths)
    runner = SubprocessRunner()
    store = Store(paths.db)
    kube = Kube(runner, settings.allowed_contexts)
    mirror = Mirror(runner, paths, settings.fleet_remote)

    forwards: list[Forward] = []
    host = settings.bastion.ssh_host
    if host:
        forwards.append(
            Forward(
                BASTION,
                "бастион",
                ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", host],
                health=kube.readyz,
                hint=f"Порты берутся из ~/.ssh/config (Host {host}). Руками: ssh -N {host}",
            )
        )
    # Проброс до Argo Server пока не делаем (решение 2026-10-21): наблюдение идёт через CRD по Kubernetes API.
    supervisor = Supervisor(forwards, subprocess_spawner, log_dir=paths.logs)

    def sync_gate() -> str | None:
        if host and supervisor.state(BASTION).status != "green":
            return "ssh-сессия к бастиону не зелёная, а pypi-mirror.gitverse.ru доступен только через неё"
        return None

    manager = RevisionManager(mirror, store, runner, paths, settings, sync_gate=sync_gate)
    observer = ClusterObserver(runner, settings, mlsc_token=lambda: read_secret(paths, "MLSC_TOKEN"))
    return AppContext(settings, paths, store, kube, manager, supervisor, observer)
