"""Submitter-под: по каждому прогону в /manifests/harbor_run — лимит агентов, очередь, живой ли сабмиттер.

Один `kubectl exec` со скриптом: строка на прогон, поля через табуляцию.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fleet_console.runner import CommandError, Runner

ROOT = "/manifests/harbor_run"
# Один проход по /proc (423 каталога × 190 процессов перебором по каждому — минута, так — секунды).
# submit_workflows[.]py: шаблон с классом, чтобы grep не нашёл собственную командную строку.
SCRIPT = f"""
cd {ROOT} 2>/dev/null || exit 0
procs=$(for f in /proc/[0-9]*/cmdline; do tr '\\0' ' ' 2>/dev/null < "$f"; echo; done | grep 'submit_workflows[.]py')
for d in */; do
  r="${{d%/}}"
  limit=$(cat "$r/agents_limit" 2>/dev/null)
  p=$(ls "$r/pending"/*.yaml 2>/dev/null | wc -l)
  s=$(ls "$r/submitted"/*.yaml 2>/dev/null | wc -l)
  c=$(ls "$r/completed"/*.yaml 2>/dev/null | wc -l)
  case "$procs" in *"submit_workflows.py {ROOT}/$r/"*) alive=1;; *) alive=0;; esac
  m=$(stat -c %Y "$r/run_logs.txt" 2>/dev/null)
  printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "$r" "$limit" "$p" "$s" "$c" "$alive" "$m"
done
"""


@dataclass(frozen=True)
class SubmitterRun:
    run_id: str
    limit: int | None  # None — файла agents_limit нет
    pending: int
    submitted: int
    completed: int
    alive: bool  # процесс submit_workflows.py этого прогона жив
    log_at: datetime | None  # mtime run_logs.txt


def parse(text: str) -> list[SubmitterRun]:
    runs = []
    for line in text.splitlines():
        if not line.strip():
            continue
        run_id, limit, p, s, c, alive, mtime = (line.split("\t") + [""] * 7)[:7]
        runs.append(
            SubmitterRun(
                run_id,
                int(limit) if limit.strip().lstrip("-").isdigit() else None,
                int(p or 0),
                int(s or 0),
                int(c or 0),
                alive.strip() == "1",
                datetime.fromtimestamp(int(mtime), tz=timezone.utc) if mtime.strip().isdigit() else None,
            )
        )
    return sorted(runs, key=lambda r: (r.log_at is not None, r.log_at), reverse=True)


async def list_runs(runner: Runner, namespace: str, pod: str) -> list[SubmitterRun]:
    result = await runner.run(["kubectl", "exec", "-n", namespace, pod, "--", "sh", "-c", SCRIPT], timeout=60)
    if not result.ok:
        raise CommandError(result)
    return parse(result.stdout)


async def log_tail(runner: Runner, namespace: str, pod: str, run_id: str, lines: int = 80) -> str:
    result = await runner.run(
        ["kubectl", "exec", "-n", namespace, pod, "--", "tail", "-n", str(lines), f"{ROOT}/{run_id}/run_logs.txt"], timeout=30
    )
    if not result.ok:
        raise CommandError(result)
    return result.stdout
