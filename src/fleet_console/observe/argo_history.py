"""Суточная история завершённых Workflow. Курсор Kubernetes и события фиксируются вместе."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable, Iterable
from urllib.parse import quote, urlencode, urlsplit

from fleet_console.observe import s3
from fleet_console.observe._time import parse_k8s_time
from fleet_console.runner import Runner
from fleet_console.settings import scrub

DAY = 86400
TERMINAL = {"Succeeded", "Failed", "Error"}
SELECTOR = "workflows.argoproj.io/completed=true"


def clean(text: str, secrets: Iterable[str]) -> str:
    text = scrub(text, secrets)
    text = re.sub(r"(?i)(Bearer\s+)[^\s,;]+", r"\1<secret>", text)
    return re.sub(r"(?i)((?:password|token|api[_-]?key|secret[_-]?key)\s*[=:]\s*)[^\s,;]+", r"\1<secret>", text)[:1200]


class ArgoHistory:
    def __init__(self, conn: sqlite3.Connection, runner: Runner, secrets: Callable[[], dict[str, str]]):
        self.conn = conn
        self.runner = runner
        self.secrets = secrets
        self.error = ""

    def checkpoint(self, scope: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT rv,since FROM argo_watch WHERE scope=?", (scope,)).fetchone()

    def save(self, scope: str, objects: list[dict], rv: str | None, *, snapshot: bool = False) -> None:
        now = int(time.time())
        rows = []
        secrets = self.secrets().values() if objects else ()
        for obj in objects:
            meta, status = obj["metadata"], obj.get("status", {})
            phase = status.get("phase")
            end = parse_k8s_time(status.get("finishedAt") or "")
            if phase not in TERMINAL or not end or end.timestamp() < now - DAY:
                continue
            nodes = (status.get("nodes") or {}).values()
            errors = sorted(
                (
                    n
                    for n in nodes
                    if n.get("type") == "Pod" and n.get("phase") in {"Failed", "Error"} and n.get("message")
                ),
                key=lambda n: n.get("finishedAt") or "",
            )
            name = meta["name"]
            run = (meta.get("labels") or {}).get("fleet/run-name") or name
            uri = (meta.get("annotations") or {}).get("fleet/results-s3", "")
            # В SQLite попадает только путь к результатам задачи без параметров и учётных данных.
            if not re.fullmatch(r"s3://[A-Za-z0-9.-]+/[\w./+=@%-]+", uri) or "/results/" not in uri:
                uri = ""
            rows.append(
                (
                    scope,
                    meta["uid"],
                    name,
                    run,
                    phase,
                    int(end.timestamp()),
                    clean(status.get("message") or "", secrets),
                    clean(errors[0]["message"], secrets) if errors else "",
                    uri,
                )
            )
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO argo_history(scope,uid,name,run,phase,finished,message,node_error,results_uri)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,uid) DO UPDATE SET
                phase=excluded.phase, finished=excluded.finished, run=excluded.run,
                results_uri=coalesce(nullif(excluded.results_uri,''), results_uri),
                message=coalesce(nullif(excluded.message,''), message),
                node_error=coalesce(nullif(excluded.node_error,''), node_error)
            """,
                rows,
            )
            self.conn.execute("DELETE FROM argo_history WHERE finished < ?", (now - DAY,))
            if rv is None:
                return
            self.conn.execute(
                """
                INSERT INTO argo_watch VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET
                rv=excluded.rv, since=CASE WHEN ? THEN excluded.since ELSE since END
            """,
                (scope, rv, now, snapshot),
            )

    async def collect(self, context: str, namespace: str) -> None:
        scope = f"{context}/{namespace}"
        checkpoint = self.checkpoint(scope)
        rv = checkpoint["rv"] if checkpoint else ""
        url = f"/apis/argoproj.io/v1alpha1/namespaces/{quote(namespace, safe='')}/workflows"
        params = {"labelSelector": SELECTOR}
        if rv:
            params.update(watch="true", resourceVersion=rv, allowWatchBookmarks="true", timeoutSeconds="8")
        else:
            params["limit"] = "100"
        objects = []
        while True:
            result = await self.runner.run(
                ["kubectl", "--context", context, "get", "--raw", url + "?" + urlencode(params)], timeout=30
            )
            if not result.ok:
                if any(
                    marker in result.stderr.lower()
                    for marker in ("(resourceexpired)", "(gone)", "too old resource version")
                ):
                    self.save(scope, [], "", snapshot=True)
                raise RuntimeError(clean(result.stderr or "Kubernetes API недоступен", self.secrets().values()))
            if rv:
                cursor = rv
                for line in result.stdout.splitlines():
                    event = json.loads(line)
                    obj = event["object"]
                    if event["type"] == "ERROR":
                        if obj.get("code") == 410:
                            self.save(scope, objects, "", snapshot=True)
                        raise RuntimeError("Kubernetes прервал watch: " + str(obj.get("reason", "ошибка")))
                    cursor = obj["metadata"]["resourceVersion"]
                    if event["type"] != "BOOKMARK":
                        objects.append(obj)
                self.save(scope, objects, cursor)
                return
            page = json.loads(result.stdout)
            token = page["metadata"].get("continue")
            self.save(scope, page["items"], None if token else page["metadata"]["resourceVersion"], snapshot=True)
            if not token:
                return
            params["continue"] = token

    def groups(self, scope: str, cutoff: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT run, sum(phase='Succeeded') AS succeeded, sum(phase!='Succeeded') AS failed,
                   max(finished) AS last FROM argo_history WHERE scope=? AND finished>=?
            GROUP BY run ORDER BY last DESC,run
        """,
            (scope, cutoff),
        ).fetchall()

    def workflows(self, scope: str, cutoff: int, run: str, offset: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT name,phase,finished FROM argo_history WHERE scope=? AND finished>=? AND run=?
            ORDER BY finished DESC,name LIMIT 51 OFFSET ?
        """,
            (scope, cutoff, run, offset),
        ).fetchall()

    def find(self, scope: str, cutoff: int, name: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM argo_history WHERE scope=? AND name=? AND finished>=? ORDER BY finished DESC LIMIT 1
        """,
            (scope, name, cutoff),
        ).fetchone()

    async def pipeline_error(self, row: sqlite3.Row, env: dict[str, str]) -> str:
        """Номер tryN общий для повторных сабмитов задачи. Читаем ошибку попытки с подом искомого Workflow."""
        if row["pipeline"] or not row["results_uri"]:
            return row["pipeline"]
        uri = urlsplit(row["results_uri"])
        prefix = uri.path.strip("/") + "/"
        result = await self.runner.run(
            [
                "aws",
                "s3api",
                "list-objects-v2",
                "--bucket",
                uri.netloc,
                "--prefix",
                prefix,
                "--delimiter",
                "/",
                "--query",
                "CommonPrefixes[].Prefix",
                "--output",
                "json",
            ],
            env=env,
            timeout=30,
        )
        if not result.ok:
            raise RuntimeError("Не удалось прочитать список попыток в S3")
        tries = sorted(
            int(key[len(prefix) + 3 : -1])
            for key in json.loads(result.stdout) or []
            if key.startswith(prefix) and re.fullmatch(r"try[0-9]+/", key[len(prefix) :])
        )
        for number in tries:
            root = row["results_uri"].rstrip("/") + f"/try{number}"
            info = await s3.cat(self.runner, root + "/debug/node_info.json", env, max_bytes=8192)
            try:
                pod = json.loads(info).get("pod_name") or ""
            except (ValueError, AttributeError):
                continue
            if pod != row["name"] and not pod.startswith(row["name"] + "-"):
                continue
            log = await s3.cat(self.runner, root + "/pipeline.log", env)
            failure = next((line for line in log.splitlines() if "[fail]" in line), "")
            if failure:
                reason = clean(failure, self.secrets().values())
                with self.conn:
                    self.conn.execute(
                        "UPDATE argo_history SET pipeline=? WHERE scope=? AND uid=?", (reason, row["scope"], row["uid"])
                    )
                return reason
        return ""
