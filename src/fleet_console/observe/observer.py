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

    @property
    def sources(self) -> list[Source]:
        return [self.runs, self.mlsc, self.nginx, self.submitter, self.nodes]

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

    async def _fill(self, source: Source, coro: Awaitable[Any]) -> None:
        try:
            source.data = await coro
            source.error = ""
        except Exception as exc:  # noqa: BLE001 — любая ошибка источника показывается в его карточке
            source.error = str(exc)
        source.at = datetime.now(timezone.utc)
        self.version += 1

    async def _read_mlsc(self) -> list[mlsc.Version]:
        token = self.mlsc_token()
        if not token:
            raise RuntimeError("нет MLSC_TOKEN: задайте в окружении сервиса или в secrets.env")
        c = self.settings.cluster
        if self._mlsc_pod is None:
            self._mlsc_pod = await mlsc.find_pod(self.runner, c.mlsc_namespace, c.mlsc_deployment)
        try:
            configuration = await mlsc.get_configuration(self.runner, c.mlsc_namespace, self._mlsc_pod, c.mlsc_base_url, token)
        except Exception:
            self._mlsc_pod = None  # под мог быть передеплоен: в следующий раз ищем заново
            raise
        return mlsc.active_versions(configuration)
