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
from fleet_console.control import Command, StopReport, stop_run, write_agent_limit
from fleet_console.control.mlsc import set_planned_state
from fleet_console.fleetref.git import Mirror
from fleet_console.fleetref.manager import PrepareError, RevisionManager
from fleet_console.guard import SleepGuard
from fleet_console.observe import ClusterObserver
from fleet_console.runner import CommandError, Runner, SubprocessRunner
from fleet_console.settings import Paths, Settings, default_home, read_secret
from fleet_console.store import Store

BASTION = "bastion"
KUBE_POLL_SEC = 30.0
KUBE_RETRY_SEC = 5.0  # пока API-сервер не ответил (ssh ещё поднимается) — опрашиваем чаще
GUARD_POLL_SEC = 15.0


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
    guard: SleepGuard = field(default_factory=SleepGuard)
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
            asyncio.create_task(self._guard_loop(), name="guard"),
        ]
        if self.settings.auto_fetch_minutes > 0:
            self._tasks.append(asyncio.create_task(self._fetch_loop(), name="auto-fetch"))

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.supervisor.stop()
        await self.guard.stop()
        await self.http.aclose()
        self.store.close()

    # --- управление своими прогонами ------------------------------------
    @property
    def runner(self) -> Runner:
        return self.observer.runner

    async def set_agent_limit(self, run_id: str, limit: int) -> Command:
        """Записать лимит агентов прогона на submitter-под; действие и команда попадают в журнал."""
        c = self.settings.cluster
        try:
            cmd = await write_agent_limit(self.runner, self.settings.namespace, c.submitter_pod, run_id, limit)
        except CommandError as exc:
            self.store.event("agent_limit", run_id, "error", str(exc))
            raise
        self.store.event("agent_limit", run_id, "ok", f"limit={limit}; {cmd.text}")
        return cmd

    async def set_replicas(self, version_id: int, count: int) -> str:
        """Плановое число инстансов версии MLSC: реплики на лету или 0 — погасить."""
        c = self.settings.cluster
        try:
            token = self.observer.require_token()
            pod = await self.observer.mlsc_pod()
            out = await set_planned_state(self.runner, c.mlsc_namespace, pod, c.mlsc_base_url, token, version_id, count)
        except (CommandError, RuntimeError) as exc:
            self.store.event("set_replicas", str(version_id), "error", str(exc))
            raise
        self.store.event("set_replicas", str(version_id), "ok", f"planned={count}")
        return out

    async def stop_own_run(self, run_id: str, version_id: int | None = None) -> StopReport:
        """Остановка прогона по документу: лимит 0, сабмиттер, Terminate workflows, затем модель (если своя)."""
        report = await stop_run(self.runner, self.settings.namespace, self.settings.cluster.submitter_pod, run_id)
        if version_id is not None:
            try:
                await self.set_replicas(version_id, 0)
                report.steps.append(f"модели (версия {version_id}) выставлено 0 инстансов")
            except (CommandError, RuntimeError) as exc:
                report.failed.append(f"модель: {exc}")
        self.store.event("stop_run", run_id, "ok" if report.ok else "error", "; ".join(report.steps + report.failed))
        return report

    # --- защита от сна ---------------------------------------------------
    def sleep_reasons(self) -> dict[str, str]:
        """Почему Mac не должен спать: свои прогоны с идущими задачами (с сессиями моделей — и они)."""
        own = self.store.own_runs()
        return {
            r.run_name: f"прогон {r.run_name}: задач идёт {r.active}"
            for r in (self.observer.runs.data or [])
            if r.run_name in own and r.active
        }

    async def _guard_loop(self) -> None:
        while True:
            await self.guard.update(self.sleep_reasons())
            await self.guard.refresh_battery()
            await asyncio.sleep(GUARD_POLL_SEC)

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
    guard = SleepGuard(runner, subprocess_spawner, log_path=paths.logs / "caffeinate.log")
    return AppContext(settings, paths, store, kube, manager, supervisor, observer, guard=guard)
