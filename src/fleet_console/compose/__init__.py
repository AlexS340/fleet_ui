"""Сборка бенч-прогона: exp.yaml и overrides, реестр датасетов, превью «что и где», preflight, исходники конфигов."""

from fleet_console.compose.preflight import Check, Issue, load_issues, required_steps, run_checks, summary
from fleet_console.compose.preview import Preview, from_resolved, tasks_total
from fleet_console.compose.registry import Mapping, build_registry, find_mapping
from fleet_console.compose.sources import (
    Drift,
    SourceFile,
    copy_for_edit,
    manual_edits,
    overlay_origin,
    save_to_overlay,
    sources,
    three_way,
    unified_diff,
    validate_yaml,
)
from fleet_console.compose.spec import BenchSpec, SessionLink, make_bench_id

__all__ = [
    "BenchSpec",
    "Check",
    "Drift",
    "Issue",
    "Mapping",
    "Preview",
    "SessionLink",
    "SourceFile",
    "build_registry",
    "copy_for_edit",
    "find_mapping",
    "from_resolved",
    "load_issues",
    "make_bench_id",
    "manual_edits",
    "overlay_origin",
    "required_steps",
    "run_checks",
    "save_to_overlay",
    "sources",
    "summary",
    "tasks_total",
    "three_way",
    "unified_diff",
    "validate_yaml",
]
