"""Executed in the submitter pod. Validate the entire selection before moving manifests."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import yaml


def prepare(root: Path, failed: set[str], completed: set[str]) -> None:
    pending = root / "pending"
    submitted = root / "submitted"
    if not pending.is_dir() or not submitted.is_dir() or not failed:
        raise ValueError("Нет очереди сабмиттера или списка неудачных попыток")
    found: set[str] = set()
    moves: list[Path] = []
    for folder in (pending, submitted):
        for path in folder.glob("*.yaml"):
            meta = yaml.safe_load(path.read_text())["metadata"]
            uri = meta["annotations"]["fleet/results-s3"].rstrip("/")
            if meta["labels"]["fleet/run-name"] != root.name:
                raise ValueError("В каталоге обнаружен манифест другого прогона")
            if folder == submitted and uri in completed:
                continue
            if uri not in failed or uri in found:
                raise ValueError("Очередь сабмиттера не соответствует выбранным попыткам")
            found.add(uri)
            if folder == submitted:
                if (pending / path.name).exists():
                    raise ValueError("Конфликт имён манифестов в очереди")
                moves.append(path)
    if found != failed:
        raise ValueError(f"Не найдены манифесты для {len(failed - found)} неудачных попыток")
    for path in moves:
        path.rename(pending / path.name)


if __name__ == "__main__":
    selection = json.load(sys.stdin)
    prepare(Path(sys.argv[1]), set(selection["failed"]), set(selection["completed"]))
