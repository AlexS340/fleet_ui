"""Защита от сна: `caffeinate -i -s -w <pid сервиса>`, пока есть хоть одна причина не спать.

Причины — активные свои прогоны и сессии моделей; их пересчитывает цикл сервиса. `-w` снимает
запрет, если сервис упал. Закрытую крышку и разряд батареи caffeinate не предотвратит, поэтому
состояние батареи (`pmset -g batt`) показывается в шапке предупреждением.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path
import re
import sys

from fleet_console.connect.supervisor import ProcessLike, Spawner, subprocess_spawner
from fleet_console.runner import Runner

PERCENT = re.compile(r"(\d+)%")
SLEEP_GAP_SEC = 120.0  # разрыв между циклами сверх периода опроса, который сон по простою не объяснит


@dataclass
class SleepReport:
    """Сводка после пробуждения: сколько спал и что за это время изменилось."""

    minutes: int
    at: datetime
    lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Battery:
    on_ac: bool
    percent: int | None


def parse_pmset(text: str) -> Battery:
    """`pmset -g batt`: источник питания в первой строке, процент — в строке батареи."""
    match = PERCENT.search(text)
    return Battery("AC Power" in text, int(match.group(1)) if match else None)


class SleepGuard:
    def __init__(
        self,
        runner: Runner | None = None,
        spawner: Spawner = subprocess_spawner,
        *,
        platform: str = sys.platform,
        pid: int | None = None,
        log_path: Path | None = None,
    ) -> None:
        self.runner = runner
        self.spawner = spawner
        self.platform = platform
        self.pid = pid or os.getpid()
        self.log_path = log_path
        self.reasons: dict[str, str] = {}  # ключ → подпись: «прогон run-a: задач идёт 12»
        self.proc: ProcessLike | None = None
        self.battery: Battery | None = None
        self.error = ""
        self.last_tick: datetime | None = None
        self.last_sleep: SleepReport | None = None  # показывается в шапке, пока не закрыли

    @property
    def supported(self) -> bool:
        return self.platform == "darwin"

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    @property
    def argv(self) -> list[str]:
        return ["caffeinate", "-i", "-s", "-w", str(self.pid)]

    async def update(self, reasons: dict[str, str]) -> None:
        """Первая причина — запустить caffeinate; последняя ушла — снять; умерший процесс перезапустить."""
        self.reasons = dict(reasons)
        if not self.reasons:
            self._kill()
            return
        if self.supported and not self.active:
            try:
                self.proc = await self.spawner(self.argv, self.log_path)
                self.error = ""
            except Exception as exc:  # noqa: BLE001 — нет caffeinate или не запустился: показываем в шапке
                self.proc = None
                self.error = f"caffeinate: {exc}"

    def note_tick(self, now: datetime, poll_seconds: float) -> int | None:
        """Цикл сервиса пишет метки времени; большой разрыв между ними — Mac спал. Возвращает минуты сна."""
        gap = None
        if self.last_tick is not None:
            extra = (now - self.last_tick).total_seconds() - poll_seconds
            if extra >= SLEEP_GAP_SEC:
                gap = max(1, round(extra / 60))
        self.last_tick = now
        return gap

    async def refresh_battery(self) -> None:
        if not self.supported or self.runner is None:
            return
        result = await self.runner.run(["pmset", "-g", "batt"], timeout=10)
        self.battery = parse_pmset(result.stdout) if result.ok else None

    def status(self) -> str:
        if not self.reasons:
            return "Защита от сна выключена: активных прогонов и сессий нет"
        if not self.supported:
            return f"Защита от сна не нужна ({self.platform}); причин не спать: {len(self.reasons)}"
        if self.active:
            return f"Защита от сна включена: {len(self.reasons)} акт."
        return "Защита от сна не поднялась" + (f" — {self.error}" if self.error else "")

    async def stop(self) -> None:
        self._kill()

    def _kill(self) -> None:
        if self.active:
            assert self.proc is not None
            self.proc.kill()
        self.proc = None
