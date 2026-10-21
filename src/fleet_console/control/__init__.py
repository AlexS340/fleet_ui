"""Управление своими объектами в кластере: лимит агентов, остановка прогона, реплики модели.

Каждое действие — короткая команда kubectl с понятным текстом для карточки и журнала; все они
разрушительные и вызываются только после диалога со сводкой и только для своих прогонов.
"""

from fleet_console.control.runs import (
    Command,
    StopReport,
    active_workflows,
    kill_submitter,
    stop_run,
    terminate_workflows,
    write_agent_limit,
)

__all__ = [
    "Command",
    "StopReport",
    "active_workflows",
    "kill_submitter",
    "stop_run",
    "terminate_workflows",
    "write_agent_limit",
]
