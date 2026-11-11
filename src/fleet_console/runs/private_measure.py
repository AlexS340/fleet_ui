"""Адаптер шага fleet: штатный read-only замер Benchboard и JSON на локальном диске.

Копируется в каталог прогона и импортируется CLI fleet из его собственного venv.
Не импортирует Fleet Console и не пишет в S3/общий каталог.
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel


class PrivateMeasureRunner(BaseModel):
    run_uri: str
    run_id: str
    sha: str
    output_file: str
    framework_version: str | None = None
    leaf_workers: int = 4
    task_workers: int = 16
    read_only: Literal[True] = True

    def run(self) -> None:
        from benchboard.measure import measure_run

        print(f"Measure-run: {self.run_uri}")
        measured = measure_run(
            self.run_uri, framework_version=self.framework_version, leaf_workers=self.leaf_workers,
            task_workers=self.task_workers, read_only=True, progress=print,
        )
        results = []
        for item in measured:
            s = item.summary
            results.append({
                "leaf": f"{s.benchmark}/{s.framework}", "score": s.score,
                "planned": s.planned_tasks, "infra_errors": s.error_tasks,
                "timeouts": s.timeout_tasks, "unattempted": s.no_attempt_tasks,
                "succeeded": s.resolved_tasks, "reward_sum": s.reward_sum,
            })
        target = Path(self.output_file)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"run_id": self.run_id, "sha": self.sha, "results": results}, ensure_ascii=False))
        temporary.replace(target)
        print(f"Measure-run done: {len(results)} leaf(s)")
