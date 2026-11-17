"""ClusterObserver: периодически читает все источники; ошибка одного не прячет остальные."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fleet_console.observe import argo, mlsc, nginx, nodes, submitter
from fleet_console.runner import Runner
from fleet_console.settings import Settings


@dataclass
class Source:
    """Последний результат одного источника."""

    title: str
    data: Any = None
    error: str = ""
    at: datetime | None = None


class ClusterObserver:
    def __init__(
        self,
        runner: Runner,
        settings: Settings,
        *,
        mlsc_token: Callable[[], str | None],
        ready: Callable[[], bool] = lambda: True,
    ) -> None:
        self.runner = runner
        self.settings = settings
        self.mlsc_token = mlsc_token
        self.ready = ready  # опрашивать только при живом API-сервере
        self.runs = Source("Прогоны Argo")
        self.mlsc = Source("Модели MLSC")
        self.nginx = Source("Шлюз nginx")
        self.submitter = Source("Сабмиттер")
        self.nodes = Source("Узлы пула")
        self.version = 0  # растёт по каждому ответившему источнику: UI перерисовывается по изменению, не ждёт самый медленный
        self.busy = False
        self._mlsc_pod: str | None = None
        self._mlsc_versions: dict[int, mlsc.Version] = {}

    @property
    def sources(self) -> list[Source]:
        return [self.runs, self.mlsc, self.nginx, self.submitter, self.nodes]

    def current(self, source: Source, *, since: str | None = None) -> Any:
        if not self.ready() or source.error:
            return None
        if since and source.at:
            cutoff = datetime.fromisoformat(since)
            if source.at < (cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)):
                return None
        return source.data

    def active_model(self, run_id: str, version_id: int | None, *, since: str | None = None) -> mlsc.Version | None:
        versions = self.current(self.mlsc, since=since) or []
        if version_id is not None:
            return next((version for version in versions if version.version_id == version_id), None)
        matches = [version for version in versions if mlsc.console_run_id(version.application) == run_id]
        return matches[0] if len(matches) == 1 else None

    def model_version(self, version_id: int) -> mlsc.Version | None:
        if self.current(self.mlsc) is None:
            return None
        return self._mlsc_versions.get(version_id)

    async def refresh(self) -> None:
        if self.busy:
            return
        self.busy = True
        c = self.settings.cluster
        try:
            await asyncio.gather(
                self._fill(self.runs, argo.list_runs(self.runner, self.settings.namespace)),
                self._fill(self.mlsc, self._read_mlsc()),
                self._fill(self.nginx, nginx.model_replicas(self.runner, c.mlsc_namespace, c.nginx_configmap, c.nginx_key)),
                self._fill(self.submitter, submitter.list_runs(self.runner, self.settings.namespace, c.submitter_pod)),
                self._fill(self.nodes, nodes.pool_status(self.runner, c.node_selector, self.settings.namespace)),
            )
        finally:
            self.busy = False

    async def loop(self) -> None:
        while True:
            if self.ready():
                await self.refresh()
            await asyncio.sleep(self.settings.cluster.poll_seconds)

    async def submitter_log(self, run_id: str) -> str:
        c = self.settings.cluster
        return await submitter.log_tail(self.runner, self.settings.namespace, c.submitter_pod, run_id)

    async def failed_worker_log(self, version_id: int) -> str:
        token = self.require_token()
        c = self.settings.cluster
        return await mlsc.get_last_failed_log(self.runner, c.mlsc_namespace, await self.mlsc_pod(), c.mlsc_base_url, token, version_id)

    async def _fill(self, source: Source, coro: Awaitable[Any]) -> None:
        try:
            source.data = await coro
            source.error = ""
        except Exception as exc:  # noqa: BLE001 — любая ошибка источника показывается в его карточке
            source.error = str(exc)
        source.at = datetime.now(timezone.utc)
        self.version += 1

    def require_token(self) -> str:
        token = self.mlsc_token()
        if not token:
            raise RuntimeError("нет MLSC_TOKEN: задайте в окружении сервиса или в secrets.env")
        return token

    async def mlsc_pod(self) -> str:
        """Живой под для вызовов MLSC; кешируется до первой ошибки (под мог быть передеплоен)."""
        c = self.settings.cluster
        if self._mlsc_pod is None:
            self._mlsc_pod = await mlsc.find_pod(self.runner, c.mlsc_namespace, c.mlsc_deployment)
        return self._mlsc_pod

    async def _read_mlsc(self) -> list[mlsc.Version]:
        token = self.require_token()
        c = self.settings.cluster
        pod = await self.mlsc_pod()
        try:
            configuration = await mlsc.get_configuration(self.runner, c.mlsc_namespace, pod, c.mlsc_base_url, token)
        except Exception:
            self._mlsc_pod = None  # под мог быть передеплоен: в следующий раз ищем заново
            raise
        self._mlsc_versions = mlsc.parse_versions(configuration)
        return [v for v in self._mlsc_versions.values() if v.planned or v.current]
