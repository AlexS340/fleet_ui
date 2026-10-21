"""Защита Mac от сна, детектор сна и предупреждения о батарее."""

from fleet_console.guard.sleep import Battery, SleepGuard, SleepReport, parse_pmset

__all__ = ["Battery", "SleepGuard", "SleepReport", "parse_pmset"]
