"""Сборка сервиса: настройки, журнал, kube, зеркало, супервизор, менеджер ревизий."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from fleet_console.catalog import CatalogEntry, scan
from fleet_console.connect.kube import Kube, KubeStatus, required_checks
from fleet_console.connect.supervisor import Forward, Supervisor, subprocess_spawner
from fleet_console.fleetref.git import Mirror
from fleet_console.fleetref.manager import PrepareError, RevisionManager
from fleet_console.runner import SubprocessRunner
from fleet_console.settings import Paths, Settings, default_home
from fleet_console.store import Store

BASTION = "bastion"
ARGO = "argo"
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
    kube_status: KubeStatus = field(default_factory=KubeStatus)
    _tasks: list[asyncio.Task] = field(default_factory=list)

    def catalog(self, worktree: Path) -> list[CatalogEntry]:
        return scan(worktree)

    def needs_permission_check(self) -> bool:
        """Права проверяем, пока их не знаем или пока хоть одно «✗»: отказ бывает транзитным (ssh только поднялся)."""
        return not self.kube_status.permissions or bool(self.kube_status.missing_permissions)

    async def refresh_kube(self, with_permissions: bool = False) -> None:
        previous = self.kube_status
        checks = required_checks(self.settings.namespace, self.settings.argo.namespace) if with_permissions else None
        status = await self.kube.status(checks)
        if not with_permissions and previous.permissions:
            status.permissions = previous.permissions  # права не зависят от связи: показываем последние известные
            if status.missing_permissions and not status.error:
                status.error = "нет прав: " + ", ".join(status.missing_permissions)
        self.kube_status = status

    async def startup(self) -> None:
        self._tasks = [
            asyncio.create_task(self.supervisor.run(), name="supervisor"),
            asyncio.create_task(self._kube_loop(), name="kube"),
            asyncio.create_task(self._mirror_init(), name="mirror"),
        ]
        if self.settings.auto_fetch_minutes > 0:
            self._tasks.append(asyncio.create_task(self._fetch_loop(), name="auto-fetch"))

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.supervisor.stop()
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
    argo = settings.argo
    argo_url = f"http://127.0.0.1:{argo.local_port}/"

    async def argo_health() -> bool:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(argo_url)
        return response.status_code in (200, 401)  # 401 = сервер жив, нужен токен

    forwards.append(
        Forward(
            ARGO,
            "Argo",
            ["kubectl", "-n", argo.namespace, "port-forward", f"service/{argo.service}", f"{argo.local_port}:{argo.remote_port}"],
            health=argo_health,
            depends_on=BASTION if host else None,
            hint=f"Argo UI: {argo_url} (auth-mode=client: нужен токен Kubernetes)",
        )
    )
    supervisor = Supervisor(forwards, subprocess_spawner, log_dir=paths.logs)

    def sync_gate() -> str | None:
        if host and supervisor.state(BASTION).status != "green":
            return "ssh-сессия к бастиону не зелёная, а pypi-mirror.gitverse.ru доступен только через неё"
        return None

    manager = RevisionManager(mirror, store, runner, paths, settings, sync_gate=sync_gate)
    return AppContext(settings, paths, store, kube, manager, supervisor)
