"""Стенд Ф5 для NiceGUI User, с двумя пользователями и фейковыми внешними сервисами."""

import fakes
from nicegui import ui

from fleet_console.app import create_app
from fleet_console.auth import TestIdentityAdapter
from fleet_console.compose.local_datasets import LocalDataset
from fleet_console.store import BenchRun, Session

fakes.CTX = ctx = fakes.make_context()
ctx.settings.auth.mode = "test"
ctx.settings.auth.legacy_owner = "alice"
now = fakes.NOW.isoformat()
for owner, score in (("alice", 75), ("bob", 50)):
    run_id = f"{owner}-cowork"
    ctx.store.create_bench(BenchRun(
        id=run_id, status="done", created_at=now, sha=fakes.SHA_MASTER, model="GLM", dataset="cowork",
        dir=str(ctx.paths.runs / run_id), owner_id="@local" if owner == "alice" else owner,
        preview_json=f'{{"s3":"s3://fake/runs/{run_id}"}}',
    ))
    ctx.store.create_session(Session(id=f"{owner}-model", recipe="glm", status="stopped", created_at=now, owner_id=owner))
    ctx.local_datasets.save(LocalDataset(
        id=f"local-{owner}", name=f"Набор {owner}", source="/fake/source", path="/fake/dataset",
        imported_at=now, tasks=["t1"], size=1, warnings=[], owner_id=owner,
    ))
    ctx.store.save_measurement(run_id, fakes.SHA_MASTER, now, [{
        "leaf": "cowork/terminus-2", "score": score, "succeeded": 3 if owner == "alice" else 2, "planned": 4,
        "infra_errors": 1, "timeouts": 0, "unattempted": 0,
    }])
ctx.store.reserve_draft("alice-draft", "@local")
create_app(ctx, TestIdentityAdapter({"alice": "alice-test", "bob": "bob-test"}))
ui.run(reload=False, show=False)
