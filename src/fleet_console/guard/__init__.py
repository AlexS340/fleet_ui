"""Защита Mac от сна и предупреждения о батарее."""

from fleet_console.guard.sleep import Battery, SleepGuard, parse_pmset

__all__ = ["Battery", "SleepGuard", "parse_pmset"]
