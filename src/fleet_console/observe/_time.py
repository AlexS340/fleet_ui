"""Разбор времени Kubernetes (RFC3339 UTC, «2026-10-21T05:00:00Z»)."""

from __future__ import annotations

from datetime import datetime, timezone


def parse_k8s_time(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.strip()).astimezone(timezone.utc)
    except ValueError:
        return None
