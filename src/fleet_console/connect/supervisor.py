"""Супервизор долгоживущих пробросов: ssh к бастиону, kubectl port-forward.

Правила: процесс умер → перезапуск с экспоненциальной паузой; проверка здоровья
провалена дважды подряд после льготного периода → kill и перезапуск; большой
разрыв между тиками (Mac спал) → перезапуск всех; проброс с depends_on ждёт,
пока зависимость не станет зелёной.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Protocol

Health = Callable[[], Awaitable[bool]]


class ProcessLike(Protocol):
    returncode: int | None

    def kill(self) -> None: ...


Spawner = Callable[[Sequence[str], Path | None], Awaitable[ProcessLike]]


async def subprocess_spawner(argv: Sequence[str], log_path: Path | None) -> ProcessLike:
    """Запуск процесса, stdout+stderr — в лог (append)."""
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            return await asyncio.create_subprocess_exec(
                *argv, stdout=log, stderr=asyncio.subprocess.STDOUT, start_new_session=True
            )
    return await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True
    )


@dataclass
class Forward:
    name: str
    title: str
    argv: list[str]
    health: Health
    depends_on: str | None = None
    hint: str = ""  # что сделать руками, если не поднимается


@dataclass
class ForwardState:
    status: str = "off"  # off | yellow | green | red
    note: str = ""
    attempt: int = 0
    fails: int = 0
    next_try: float = 0.0
    started_at: float | None = None
    last_error: str = ""
    proc: ProcessLike | None = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None


class Supervisor:
    def __init__(
        self,
        forwards: Sequence[Forward],
        spawner: Spawner = subprocess_spawner,
        *,
        log_dir: Path | None = None,
        interval: float = 5.0,
        max_backoff: float = 60.0,
        grace: float = 20.0,
        sleep_gap: float = 30.0,
        health_timeout: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.forwards = list(forwards)
        self.spawner = spawner
        self.log_dir = log_dir
        self.interval = interval
        self.max_backoff = max_backoff
        self.grace = grace
        self.sleep_gap = sleep_gap
        self.health_timeout = health_timeout
        self.clock = clock
        self.states: dict[str, ForwardState] = {f.name: ForwardState() for f in self.forwards}
        self._last_tick: float | None = None
        self._running = False

    def state(self, name: str) -> ForwardState:
        return self.states[name]

    def forward(self, name: str) -> Forward:
        return next(f for f in self.forwards if f.name == name)

    def add(self, fwd: Forward) -> None:
        """Проброс на лету (шлюз моделей по кнопке): поднимется на следующем тике."""
        if fwd.name in self.states:
            raise ValueError(f"проброс «{fwd.name}» уже есть")
        self.forwards.append(fwd)
        self.states[fwd.name] = ForwardState()

    def remove(self, name: str) -> None:
        """Убрать проброс и убить его процесс; незнакомое имя — не ошибка."""
        st = self.states.pop(name, None)
        if st is not None:
            self._kill(st)
        self.forwards = [f for f in self.forwards if f.name != name]

    def log_path(self, fwd: Forward) -> Path | None:
        return self.log_dir / f"{fwd.name}.log" if self.log_dir else None

    async def run(self) -> None:
        self._running = True
        while self._running:
            await self.tick()
            await asyncio.sleep(self.interval)

    async def stop(self) -> None:
        self._running = False
        for st in self.states.values():
            self._kill(st)
            st.status = "off"
            st.note = "остановлен"

    async def restart(self, name: str) -> None:
        st = self.states[name]
        self._kill(st)
        st.attempt = 0
        st.next_try = 0.0
        st.note = "перезапуск по запросу"

    async def tick(self) -> None:
        now = self.clock()
        if self._last_tick is not None and now - self._last_tick > self.sleep_gap:
            self._restart_all(f"разрыв {int(now - self._last_tick)} с между тиками — похоже, Mac спал")
        self._last_tick = now
        for fwd in list(self.forwards):  # копия: проброс могут убрать из UI, пока тик ждёт проверку
            if fwd.name in self.states:
                await self._tick_one(fwd, now)

    async def _tick_one(self, fwd: Forward, now: float) -> None:
        st = self.states[fwd.name]
        if fwd.depends_on and self.states[fwd.depends_on].status != "green":
            self._kill(st)
            st.status = "off"
            st.note = f"ждёт: {self.forward(fwd.depends_on).title}"
            return
        if st.proc is not None and st.proc.returncode is not None:
            st.last_error = f"процесс завершился с кодом {st.proc.returncode}" + self._log_tail(fwd)
            st.proc = None
            self._schedule_retry(st, now)
        if st.proc is None:
            if now >= st.next_try:
                await self._spawn(fwd, st, now)
            else:
                st.note = f"повтор через {max(1, int(st.next_try - now))} с (попытка {st.attempt})"
            return
        try:
            ok = await asyncio.wait_for(fwd.health(), self.health_timeout)
        except Exception as exc:  # noqa: BLE001 — любая ошибка проверки = не здоров
            ok = False
            st.last_error = f"проверка: {exc}"
        if ok:
            st.status, st.note, st.fails, st.attempt = "green", "", 0, 0
            st.started_at = None  # льготный период только до первой удачной проверки
            return
        in_grace = st.started_at is not None and now - st.started_at < self.grace
        if not in_grace:
            st.fails += 1
        st.status = "yellow"
        st.note = "запускается" if in_grace else f"проверка не прошла ({st.fails}/2)"
        if st.fails >= 2:
            self._kill(st)
            st.last_error = st.last_error or "проверка здоровья не прошла дважды подряд"
            self._schedule_retry(st, now)

    async def _spawn(self, fwd: Forward, st: ForwardState, now: float) -> None:
        try:
            st.proc = await self.spawner(fwd.argv, self.log_path(fwd))
        except Exception as exc:  # noqa: BLE001
            st.last_error = f"не удалось запустить: {exc}"
            self._schedule_retry(st, now)
            return
        st.started_at = now
        st.fails = 0
        st.status = "yellow"
        st.note = "запускается"

    def _schedule_retry(self, st: ForwardState, now: float) -> None:
        st.attempt += 1
        delay = min(2 ** (st.attempt - 1), self.max_backoff)
        st.next_try = now + delay
        st.status = "red" if st.attempt >= 6 else "yellow"
        st.note = f"повтор через {int(delay)} с (попытка {st.attempt})"

    def _restart_all(self, reason: str) -> None:
        for st in self.states.values():
            self._kill(st)
            st.attempt = 0
            st.next_try = 0.0
            st.status = "yellow"
            st.note = "перезапуск"
            st.last_error = reason

    @staticmethod
    def _kill(st: ForwardState) -> None:
        if st.alive:
            assert st.proc is not None
            st.proc.kill()
        st.proc = None

    def _log_tail(self, fwd: Forward, lines: int = 3) -> str:
        path = self.log_path(fwd)
        if not path or not path.exists():
            return ""
        tail = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-lines:]
        return (": " + " | ".join(tail)) if tail else ""
