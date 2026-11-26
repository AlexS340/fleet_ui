import asyncio
import json
from pathlib import Path
from shlex import quote

from nicegui import ui
from nicegui.testing import User
import pytest

import cluster_samples
from fleet_console.fleetref.profile import CONFIGS_REL
from fleet_console.compose import BenchSpec
from fleet_console.presets import load_presets, personal_dir
from fleet_console.runner import Result
from fleet_console.observe.mlsc import Version
from fleet_console.settings import OVERLAY_DIR, Settings
from fleet_console.store import BenchRun, Session
import fakes

pytestmark = pytest.mark.nicegui_main_file("tests/ui_main.py")


def _select(user: User, label: str) -> ui.select:
    return next(s for s in user.find(kind=ui.select).elements if s.props.get("label") == label)


async def test_stopped_session_shows_live_version_and_unavailable_mlsc(user: User) -> None:
    ctx = fakes.CTX
    await user.open("/models")
    ctx.store.create_session(Session("closed-with-model", "glm", "stopped", fakes.NOW.isoformat(), version_id=1473))
    ctx.observer.mlsc.data = [Version(1473, "fleet-console closed-with-model", "GLM", 1, 1)]
    await user.open("/models/closed-with-model")
    await user.should_see("Локальный статус расходится с MLSC. Версия 1473 ещё активна.")
    await user.should_see("Погасить")

    ctx.observer.mlsc.error = "MLSC offline"
    await user.open("/models/closed-with-model")
    await user.should_see("Актуальный статус MLSC недоступен")
    await user.should_not_see("Локальный статус расходится с MLSC")


async def test_header_and_branches(user: User) -> None:
    await user.open("/")
    await user.should_see("Fleet Console")
    await user.should_see("internal · swe-bench-v2-20260308")
    await user.should_see("бастион")
    await user.should_see("fetch:")
    branch = _select(user, "Ветка")
    assert set(branch.options) == {"master", "feat/x"} and branch.value == "master"
    await asyncio.sleep(0.05)  # on_change → загрузка коммитов
    commit = _select(user, "Коммит")
    assert commit.value == fakes.SHA_MASTER and len(commit.options) == 2
    await user.should_see("влит в master")


async def test_prepare_shows_profile_and_catalog(user: User) -> None:
    await user.open("/")
    await asyncio.sleep(0.05)
    _select(user, "Ветка").set_value("feat/x")
    await asyncio.sleep(0.05)
    await user.should_see("отстаёт от master на 3")
    user.find("Подготовить ревизию").click()
    await user.should_see("Готово: " + fakes.SHA_FEAT[:8])
    await user.should_see("Harbor 0.22.0")
    await user.should_see("Модели · mlsc/model")
    await user.should_see("Рецепт GLM-5.3-Flash на 8 GPU.")
    await user.should_see("Агент terminus-2 ходит в Polar gateway.")
    captions = {e.props.get("caption") for e in user.find(kind=ui.expansion).elements}
    assert "cowork · cowork/tasks" in captions  # summary реестра datasets — в подписи expansion
    assert fakes.CTX.manager.prepared[-1] == fakes.SHA_FEAT
    await user.should_see("профиль в кеше")


async def test_prepare_error_is_visible(user: User) -> None:
    fakes.CTX.manager.prepare_error = "uv sync невозможен: ssh-сессия к бастиону не зелёная"
    try:
        await user.open("/")
        await asyncio.sleep(0.05)
        user.find("Подготовить ревизию").click()
        await user.should_see("uv sync невозможен")
    finally:
        fakes.CTX.manager.prepare_error = None
        fakes.CTX.manager.progress.phase = "idle"


async def test_fetch_button_and_dialogs(user: User) -> None:
    await user.open("/")
    before = fakes.CTX.manager.fetched
    user.find("Актуализировать").click()
    await user.should_see("Зеркало fleet обновлено")
    assert fakes.CTX.manager.fetched == before + 1
    user.find("бастион").click()
    await user.should_see("Проброс: бастион")
    await asyncio.sleep(2.5)  # таймер шапки успевает сделать refresh — диалог должен пережить его
    await user.should_see("ssh -N gigacode-bastion")
    user.find("Перезапустить").click()
    await user.should_see("бастион: перезапуск")
    assert fakes.CTX.supervisor.restarted[-1] == "bastion"


async def test_settings_page_saves(user: User) -> None:
    await user.open("/settings")
    user.find("Квоты и хранение результатов").click()
    shared = user.find(kind=ui.checkbox).elements.pop()
    assert shared.value is False
    shared.set_value(True)
    user.find("Сохранить").click()
    await user.should_see("Сохранено")
    assert Settings.load(fakes.CTX.paths).results.allow_shared_bucket is True


async def test_cluster_page_shows_all_sources(user: User) -> None:
    await user.open("/cluster")
    await user.should_see("Кластер")
    user.find("Обновить").click()
    await user.should_see("скрыто завершённых прогонов: 2")  # активный run-a виден, run-b и безлейбловый скрыты
    await user.should_see("живых версий: 2 · GPU занято: 28")
    await user.should_see("узлов 3, готовых 2, cordoned 1")
    await user.should_see("подов в Pending: 2")
    await user.should_see("Кластер · прогонов идёт: 1 · GPU: 28")  # сводка в шапке
    tables = {t.columns[-1]["name"]: t for t in user.find(kind=ui.table).elements}  # у Argo и сабмиттера row_key одинаковый
    runs, models, route = tables["argo"], tables["gpus"], tables["replicas"]
    assert [r["run"] for r in runs.rows] == ["run-a"] and runs.rows[0]["pending"] == 1
    assert "label=fleet%2Frun-name%3Drun-a" in runs.rows[0]["argo"]
    assert [m["model"] for m in models.rows] == ["GLM-5.3-Flash", "Qwen3"] and models.rows[0]["gpus"] == 24
    assert route.rows == [{"model": "GLM-5.3-Flash", "replicas": 2}, {"model": "Qwen3", "replicas": 1}]


async def test_cluster_page_marks_own_runs(user: User) -> None:
    fakes.CTX.store.adopt_run("run-a", 41, model="GLM")
    fakes.CTX.store.adopt_run("run-gone", None, model="old")  # workflows удалены: строка нужна, чтобы прогон можно было забыть
    try:
        await user.open("/cluster")
        user.find("Обновить").click()
        await user.should_see("скрыто завершённых прогонов: 2")
        tables = {t.columns[-1]["name"]: t for t in user.find(kind=ui.table).elements}
        assert tables["argo"].rows[0]["own"] == "да"
        assert tables["argo"].rows[-1]["run"] == "run-gone" and tables["argo"].rows[-1]["own"] == "да · workflows не видно"
        assert [m["own"] for m in tables["gpus"].rows] == ["да", "—"]  # версия 41 своя, 42 чужая
    finally:
        fakes.CTX.store.forget_run("run-a")
        fakes.CTX.store.forget_run("run-gone")


async def test_models_page_gateway_and_sandbox(user: User) -> None:
    await user.open("/models")
    await user.should_see("Шлюз моделей")
    user.find("Пробросить порт").click()
    await user.should_see("http://127.0.0.1:18080/v1")
    await user.should_see("GLM-5.3-Flash (512k)")
    assert fakes.CTX.supervisor.forward("gateway-litellm").argv[-1] == "18080:80"
    await user.should_see("Модели")  # чип в шапке и ссылка
    box = next(t for t in user.find(kind=ui.textarea).elements if t.props.get("label") == "Сообщение")
    box.set_value("Привет")
    user.find("Отправить").click()
    await user.should_see("Привет, мир")
    await user.should_see("до первого токена")
    await user.should_see("finish: tool_calls")
    user.find("Закрыть проброс").click()
    await user.should_see("Пробросить порт")
    assert not fakes.CTX.gateways


async def test_header_shows_sleep_guard(user: User) -> None:
    await fakes.CTX.guard.update({"run-a": "прогон run-a: задач идёт 2"})
    try:
        await user.open("/")
        await user.should_see("без сна · 1")
        user.find("без сна · 1").click()
        await user.should_see("прогон run-a: задач идёт 2")
        await user.should_see("Защита от сна включена")
        # сводка после сна Mac — чип в шапке, закрывается кнопкой
        from datetime import datetime, timezone

        from fleet_console.guard import SleepReport

        fakes.CTX.guard.last_sleep = SleepReport(47, datetime.now(timezone.utc), ["прогон run-a: завершилось задач 120, идёт 3"])
        await user.open("/")
        await user.should_see("Mac спал 47 мин")
        user.find("Mac спал 47 мин").click()
        await user.should_see("завершилось задач 120")
        user.find("Понятно").click()
        assert fakes.CTX.guard.last_sleep is None
    finally:
        await fakes.CTX.guard.update({})
        fakes.CTX.guard.last_sleep = None


@pytest.mark.parametrize(("job_queue", "job_priority"), [("", ""), ("shared", "shared-medium"), ("gigacode", "high")])
async def test_models_page_sessions(user: User, job_queue: str, job_priority: str) -> None:
    await user.open("/models")
    await user.should_see("Сессии моделей")
    user.find("Поднять модель").click()
    await user.should_see("Сначала подготовьте совместимую ревизию")
    user.find("Закрыть").click()
    await fakes.CTX.manager.prepare("master")
    user.find("Поднять модель").click()
    await user.should_see("Рецепт mlsc/model")
    await user.should_see("в рецепте: served_model_name GLM")
    priority = _select(user, "Приоритет джобы")
    assert priority.value == ""
    queue = _select(user, "Очередь MLSC")
    assert queue.value == ""
    queue.set_value(job_queue)
    if job_queue == "shared":
        assert list(priority.options) == ["shared-low", "shared-medium"]
        assert priority.value == "shared-low"
    priority.set_value(job_priority)
    user.find("Запустить подъём").click()
    await user.should_see("Занять GPU под модель?")
    await user.should_see("стадии: preflight → mlsc.create_job")
    if job_priority:
        await user.should_see(f"приоритет джобы {job_priority}")
        await user.should_see(f"очередь MLSC {job_queue}")
    user.find("Да, поднять").click()
    await user.should_see("подъём начат")
    await asyncio.gather(*fakes.CTX.sessions.tasks.values())
    session = fakes.CTX.sessions.sessions()[0]
    assert session.status == "ready" and session.version_id == 777 and session.id.startswith("console-glm-")
    stages = fakes.CTX.sessions.stages(session.id)
    assert [s.stage for s in stages] == ["preflight", "create_job", "start_llm", "check"]
    for stage in stages[:2]:
        if not job_priority:
            assert "job_priority_class=" not in stage.command
            assert "queue_name=" not in stage.command
        else:
            assert f"++mlsc.create_job.job.job_priority_class={job_priority}" in stage.command
            assert f"++mlsc.create_job.job.queue_name={job_queue}" in stage.command
    assert "session:" + session.id in fakes.CTX.sleep_reasons()
    await fakes.CTX.sessions.stop(session.id)
    assert fakes.CTX.sessions.get(session.id).status == "stopped"


async def test_model_session_shows_failed_worker_log(user: User, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = fakes.CTX
    ctx.store.create_session(Session("failed-before-mlsc", "glm", "failed", fakes.NOW.isoformat()))
    ctx.store.create_session(Session("stopped-model", "glm", "stopped", fakes.NOW.isoformat(), version_id=1446))
    monkeypatch.setattr(ctx.benches, "secrets", lambda: {"MLSC_TOKEN": "mock-secret-value"})
    endpoint = ctx.settings.cluster.mlsc_base_url.rstrip("/") + "/api/v1/service/get_last_failed_log?application_version_id=1446"
    prefix = (
        "kubectl", "exec", "-i", "-n", "vllm-proxy", "litellm-noauth-abc", "--", "sh", "-c",
        f'curl -sSf -k -H "Authorization: Bearer $(cat)" {quote(endpoint)}',
    )
    runner = ctx.observer.runner
    monkeypatch.setattr(runner, "handlers", [(prefix, Result(prefix, 0, '{"log":"vLLM: HFValidationError mock-secret-value"}')), *runner.handlers])

    await user.open("/models")
    button = "Лог упавшего воркера модели"
    user.find(kind=ui.table).trigger("rowClick", [None, {"id": "failed-before-mlsc"}])
    await user.should_not_see(button)
    user.find("Закрыть").click()
    user.find(kind=ui.table).trigger("rowClick", [None, {"id": "stopped-model"}])
    user.find(button).click()
    await user.should_see("MLSC: последний упавший воркер · версия 1446")
    await user.should_see("vLLM: HFValidationError <secret>")
    assert any(argv == prefix and meta["stdin"] == "token" for argv, meta in runner.calls)


@pytest.mark.parametrize("legacy", [False, True])
async def test_repeat_model_session_uses_saved_setup_and_sha(user: User, legacy: bool) -> None:
    ctx = fakes.CTX
    revision = await ctx.manager.prepare("master")
    ctx.settings.quotas.user_gpus = ctx.settings.quotas.total_gpus = 32
    extra = ("++mlsc.create_job.job.env.MODEL_PATH='/models/my model'", "++mlsc.create_job.job.job_priority_class=shared-medium",
             "++mlsc.create_job.job.queue_name=shared")
    source = await ctx.sessions.start(revision, recipe="glm", model_name="GLM-personal", replicas=3,
                                      inference="nginx", ttl_minutes=85, extra=extra)
    await ctx.sessions.tasks[source.id]
    await ctx.sessions.stop(source.id)
    if legacy:
        (Path(source.dir) / "launch.json").unlink()
        import shlex
        argv = ["uv", "run", "fleet", "console_model.yaml"]
        for value in ["experiment.jobs=3", "output_dir=/old/out", "mlsc.served_model_name=GLM-personal", *extra, "main.run=[mlsc.create_job]"]:
            argv += ["-o", value]
        ctx.store.conn.execute("UPDATE session_stages SET command=? WHERE session_id=? AND stage='preflight'", (shlex.join(argv), source.id))
        ctx.store.conn.commit()
    await ctx.manager.prepare("feat/x")  # в шапке выбрана другая ревизия
    await user.open("/models")
    user.find(kind=ui.table).trigger("rowClick", [None, {"id": source.id}])
    user.find("Запустить с теми же параметрами").click()
    await user.should_see("Будет создана новая сессия")
    assert ctx.manager.prepared[-1] == source.sha
    assert _select(user, "Рецепт mlsc/model").value == "glm"
    assert _select(user, "Маршрут").value == "nginx"
    assert _select(user, "Приоритет джобы").value == "shared-medium"
    assert _select(user, "Очередь MLSC").value == "shared"
    numbers = {e.props["label"]: e for e in user.find(kind=ui.number).elements}
    assert numbers["Реплики"].value == 3 and numbers["Время жизни, мин"].value == 85
    model = next(e for e in user.find(kind=ui.input).elements if e.props.get("label", "").startswith("Имя модели"))
    assert model.value == "GLM-personal"
    overrides = next(e for e in user.find(kind=ui.textarea).elements if e.props.get("label", "").startswith("overrides fleet"))
    assert overrides.value == extra[0]
    assert len(ctx.sessions.sessions()) == 1  # открытие формы не создаёт сессию и не занимает GPU
    model.set_value("GLM-repeated")
    user.find(marker="start-session").click()
    await user.should_see("Занять GPU под модель?")
    assert len(ctx.sessions.sessions()) == 1
    user.find("Да, поднять").click()
    await user.should_see("подъём начат")
    await asyncio.gather(*ctx.sessions.tasks.values())
    repeated = next(s for s in ctx.sessions.sessions() if s.id != source.id)
    assert repeated.sha == source.sha and repeated.model.startswith("glm-repeated-")
    assert ctx.sessions.launch_options(repeated)["extra"] == extra
    assert ctx.sessions.get(source.id).model == source.model and ctx.sessions.get(source.id).status == "stopped"
    await ctx.sessions.stop(repeated.id)


async def test_repeat_model_session_shows_revision_prepare_error(user: User) -> None:
    ctx = fakes.CTX
    source = await ctx.sessions.start(await ctx.manager.prepare("master"), recipe="glm")
    await ctx.sessions.tasks[source.id]
    await ctx.sessions.stop(source.id)
    ctx.manager.prepare_error = "Не удалось подготовить исходную ревизию"
    await user.open("/models")
    user.find(kind=ui.table).trigger("rowClick", [None, {"id": source.id}])
    user.find("Запустить с теми же параметрами").click()
    await user.should_see("Не удалось подготовить исходную ревизию")
    assert len(ctx.sessions.sessions()) == 1


async def test_benches_page_form_preview_and_run(user: User) -> None:
    from fleet_console.connect.kube import KubeStatus

    fakes.CTX.kube_status = KubeStatus(context="internal", server="s", allowed=True, ready=True, permissions={"get nodes": True})
    await user.open("/runs")
    await user.should_see("Прогоны")
    await user.should_see("Прогонов ещё нет")
    user.find("Запустить бенч").click()
    await user.should_see("Сначала подготовьте совместимую ревизию")
    user.find("Закрыть").click()
    await fakes.CTX.manager.prepare("master")
    user.find("Запустить бенч").click()
    await user.should_see("ревизия master@378874ba")
    _select(user, "Пресет").set_value("qwen36-birdsql-smoke")
    await asyncio.sleep(0.05)
    await user.should_see("несовместим с ревизией: в ревизии нет mlsc/model=qwen36_35b_a3b")
    _select(user, "Пресет").set_value("")
    numbers = {n.props.get("label"): n for n in user.find(kind=ui.number).elements}
    numbers["Агентов на реплику"].set_value(2)
    numbers["Лимит задач (пусто — все)"].set_value(3)
    repeats = numbers["Попыток на задачу"]
    assert repeats.value == 1
    await user.should_see("GPU: 1 × 8 = 8 · лимит агентов 1 × 2 = 2")
    priority = _select(user, "Приоритет джобы")
    assert priority.value == ""
    queue = _select(user, "Очередь MLSC")
    assert queue.value == ""
    user.find("Превью и preflight").click()
    await user.should_see("Что и где будет запущено")
    preview = {r["k"]: r["v"] for t in user.find(kind=ui.table).elements for r in t.rows if "k" in r}
    assert preview["GPU"] == "1 × a100plus.8gpu.80vG.96C.1456G = 8 GPU" and preview["Параллелизм"] == "лимит 2 = 1 реплик × 2 агентов"
    assert " · образ fleet-vllm:0.24.0 · очередь gigacode · поднимет сервис" in preview["Модель"]
    assert preview["Приоритет джобы"] == "medium"
    (draft_dir,) = fakes.CTX.paths.runs.iterdir()
    assert "job_priority_class=" not in (draft_dir / "overrides.txt").read_text()
    assert "queue_name=" not in (draft_dir / "overrides.txt").read_text()
    await user.should_see("Preflight: ✗ 0 · ⚠ 2")  # квота GPU и terminus-2 на Harbor 0.22
    await user.should_see("tasks → cowork (fleet)")
    start = user.find(marker="start-bench").elements.pop()
    assert not start.enabled  # предупреждения не приняты
    user.find(marker="accept-warnings").elements.pop().set_value(True)
    assert start.enabled
    repeats.set_value(2.5)
    assert not start.enabled
    user.find("Превью и preflight").click()
    await user.should_see("задайте целое число от 1")
    repeats.set_value(3)
    extra = next(e for e in user.find(kind=ui.textarea).elements if e.props.get("label", "").startswith("overrides fleet"))
    extra.set_value("++mlsc.create_job.job.queue_name=shared\n++mlsc.create_job.job.job_priority_class=low")
    user.find("Превью и preflight").click()
    await user.should_see("Очередь shared не принимает приоритет low")
    assert not start.enabled
    extra.set_value("")
    queue.set_value("shared")
    assert list(priority.options) == ["shared-low", "shared-medium"]
    assert priority.value == "shared-low"
    priority.set_value("shared-medium")
    queue.set_value("gigacode")
    assert list(priority.options) == ["low", "medium", "high"]
    assert priority.value == "medium"
    queue.set_value("shared")
    assert not start.enabled
    await user.should_see("Обновите превью и preflight")
    user.find("Превью и preflight").click()
    await user.should_not_see("Обновите превью и preflight")
    preview = {r["k"]: r["v"] for t in user.find(kind=ui.table).elements for r in t.rows if "k" in r}
    assert preview["Приоритет джобы"] == "shared-low"
    assert preview["Попыток на задачу"] == "3"
    assert "очередь shared" in preview["Модель"]
    user.find(marker="accept-warnings").elements.pop().set_value(True)
    user.find(marker="start-bench").click()
    await user.should_see("Запустить прогон?")
    await user.should_see("приоритет джобы shared-low")
    await user.should_see("очередь MLSC shared")
    await user.should_see("Попыток на задачу: 3")
    await user.should_see("стадии: preflight → материализация → create_job → start_llm → check → upload → launch → исполнение → stop_llm → замер")
    user.find("Да, запустить").click()
    await user.should_see("Заявка принята")
    await asyncio.gather(*fakes.CTX.benches.tasks.values())
    run = fakes.CTX.benches.runs()[0]
    assert run.status == "done" and run.version_id == 777 and run.task_limit == 3 and run.agents_per_replica == 2 and run.id.startswith("glm-cowork-")
    stages = fakes.CTX.benches.stages(run.id)
    assert [s.stage for s in stages] == ["preflight", "materialize", "create_job", "start_llm", "check", "upload", "launch", "execute", "stop_llm", "measure"]
    assert "++mlsc.create_job.job.job_priority_class=shared-low" in stages[2].command
    assert "++mlsc.create_job.job.queue_name=shared" in stages[2].command
    assert fakes.CTX.store.get_run(run.id) is not None and run.preview["gpu_per_instance"] == 8 and run.preview["tasks_total"] is None
    exp = (fakes.CTX.paths.runs / run.id / "exp.yaml").read_text()
    assert "harbor/dataset: cowork" in exp and "task_limit: 3" in exp and (fakes.CTX.paths.runs / run.id / "resolved.yaml").exists()
    assert "  repeats: 3" in exp
    # карточка прогона по клику на строку
    await user.open("/runs")
    await user.should_see("клик по строке — карточка прогона")
    table = next(iter(user.find(kind=ui.table).elements))
    assert table.rows[0]["tasks"] == "0 / 0 / 3" and table.rows[0]["status"] == "завершён с ошибками · неудачных попыток: 1"
    (fakes.CTX.paths.runs / run.id / "out" / ".fleet").mkdir(parents=True, exist_ok=True)
    (fakes.CTX.paths.runs / run.id / "out" / ".fleet" / "tasks.json").write_text('[{"a": 1}, {"a": 2}, {"a": 3}]')  # появился после материализации
    user.find(kind=ui.table).trigger("rowClick", [None, {"id": run.id}])
    await user.should_see("Что и где запущено")
    facts = {r["k"]: r["v"] for r in next(iter(user.find(marker="bench-facts").elements)).rows}
    assert "· 3 задач ·" in facts["Задачи"]  # число задач — из tasks.json, а не из превью на момент запуска
    await user.should_see("подготовка: --print-config · код 0")
    await user.should_see("замер (Benchboard) · код 0")
    await user.should_see("Замер заново")
    await user.should_see("Журнал")
    await user.should_see("Benchboard")
    # задачи: workflows Argo + результаты в S3 (попытки tryN, флаги), фильтр «только упавшие»
    await user.should_see("workflows: 3 · задач в S3: 3, COMPLETED 1, FAILED 1")
    tasks_table = user.find(marker="bench-tasks").elements.pop()
    assert [r["task"] for r in tasks_table.rows] == ["t1-0", "t2-0", "t3-0"]
    assert tasks_table.rows[1]["phase"] == "упал" and tasks_table.rows[1]["result"] == "2 · FAILED" and tasks_table.rows[1]["duration"] == "0:05:00"
    assert tasks_table.rows[2]["result"] == "0 · попыток нет" and tasks_table.rows[0]["result"] == "1 · COMPLETED"
    user.find("только упавшие").click()
    assert [r["task"] for r in tasks_table.rows] == ["t2-0"]
    user.find(marker="bench-tasks").trigger("rowClick", [None, tasks_table.rows[0]])
    await user.should_see("S3: попыток 2 · итог FAILED")
    await user.should_see("polar_trajectories/polar_trajectories.tar.gz")
    await user.should_see("pod failed")
    user.find("показать").click()
    await user.should_see("runner log line 2")
    user.find("Лог упавшего воркера модели").click()
    await user.should_see("MLSC: последний упавший воркер")


async def test_bench_can_save_preset_and_repeat_on_original_sha(user: User) -> None:
    ctx = fakes.CTX
    revision = await ctx.manager.prepare("master")
    source = BenchSpec(
        run_id="source-run", worktree=revision.worktree, run_dir=ctx.paths.runs / "source-run", overlay=OVERLAY_DIR,
        recipe="glm", agent="terminus2", dataset="cowork", sha=revision.info.sha, task_limit=3, agents_per_replica=2,
        model_name="custom-model", inference="nginx", harbor_job={"agent_timeout_multiplier": 2},
        extra=("++experiment.repeats=4", "++mlsc.create_job.job.job_priority_class=low", "++mlsc.create_job.job.queue_name=shared", "execution.shuffle=false"),
    )
    source.write()
    (source.run_dir / "harbor" / "agent").mkdir(parents=True)
    edit = source.run_dir / "harbor" / "agent" / "terminus2.yaml"
    edit.write_text("harbor_job: {agent_timeout_multiplier: 3}\n")
    source.resolved.write_text("should-not-be-copied: true\n")
    ctx.store.create_bench(BenchRun(source.run_id, "done", fakes.NOW.isoformat(), sha=source.sha, dir=str(source.run_dir)))
    await ctx.manager.select(ctx.access.id, "feat/x")

    await user.open("/runs/source-run")
    user.find("Сохранить как пресет").click()
    name = next(e for e in user.find(kind=ui.input).elements if e.props.get("label") == "Имя пресета")
    name.set_value("smoke-copy")
    user.find(marker="save-personal-preset").click()
    await user.should_see("Пресет «smoke-copy» сохранён")
    (saved,), errors = load_presets(personal_dir(ctx.paths.home, ctx.access.id))
    assert not errors
    assert saved.fleet.ref == source.sha and saved.model_name == "custom-model"
    assert saved.config_files == {"harbor/agent/terminus2.yaml": edit.read_text()}
    assert load_presets(personal_dir(ctx.paths.home, "another-user")) == ([], [])

    user.find("Запустить с теми же параметрами").click()
    await user.should_see("Новый прогон из истории")
    await user.should_see("Что и где будет запущено")
    assert len(ctx.store.benches()) == 1
    assert ctx.manager.prepared[-1] == source.sha
    assert ctx.manager.selected(ctx.access.id).info.sha == fakes.SHA_FEAT
    assert _select(user, "Приоритет джобы").value == "shared-low"
    assert _select(user, "Очередь MLSC").value == "shared"
    assert _select(user, "Маршрут").value == "nginx"
    draft_dir, = [p for p in ctx.paths.runs.iterdir() if p != source.run_dir]
    clone = BenchSpec.from_json((draft_dir / "spec.json").read_text())
    assert clone.run_id != source.run_id and clone.effective_model != source.effective_model
    assert clone.extra == (source.extra[-1], "++mlsc.create_job.job.job_priority_class=shared-low", source.extra[-2])
    assert clone.harbor_job == source.harbor_job and clone.task_limit == source.task_limit
    assert clone.repeats == 4 and "  repeats: 4" in clone.exp.read_text()
    assert (draft_dir / edit.relative_to(source.run_dir)).read_text() == edit.read_text()
    assert "should-not-be-copied" not in (draft_dir / "resolved.yaml").read_text()

    await user.open("/runs")
    user.find("Запустить бенч").click()
    _select(user, "Пресет").set_value("personal:smoke-copy")
    await user.should_see("Параметры из «smoke-copy»")
    assert ctx.manager.prepared[-1] == source.sha
    priority_fields = [e for e in user.find(kind=ui.select).elements if e.props.get("label") == "Приоритет джобы"]
    assert max(priority_fields, key=lambda e: e.id).value == "shared-low"


async def test_bench_on_existing_session_ignores_selected_priority(user: User) -> None:
    from fleet_console.connect.kube import KubeStatus
    from fleet_console.store import Session

    await fakes.CTX.manager.prepare("master")
    fakes.CTX.kube_status = KubeStatus(context="internal", server="s", allowed=True, ready=True, permissions={"get nodes": True})
    fakes.CTX.store.create_session(Session("ready-model", "glm", "ready", fakes.NOW.isoformat(), model="GLM", version_id=777))
    await user.open("/runs")
    user.find("Запустить бенч").click()
    _select(user, "Приоритет джобы").set_value("high")
    _select(user, "Очередь MLSC").set_value("shared")
    _select(user, "Модель").set_value("session:ready-model")
    await user.should_see("модель уже поднята: сессия GLM")
    await user.should_not_see("Приоритет джобы")
    await user.should_not_see("Очередь MLSC")
    user.find("Превью и preflight").click()
    await user.should_see("Что и где будет запущено")
    (draft_dir,) = fakes.CTX.paths.runs.iterdir()
    assert "job_priority_class=" not in (draft_dir / "overrides.txt").read_text()
    assert "queue_name=" not in (draft_dir / "overrides.txt").read_text()
    preview = {r["k"]: r["v"] for t in user.find(kind=ui.table).elements for r in t.rows if "k" in r}
    assert "сессия ready-model" in preview["Модель"] and "Приоритет джобы" not in preview


async def test_benches_page_full_config_and_manual_edit(user: User) -> None:
    from fleet_console.connect.kube import KubeStatus

    fakes.CTX.kube_status = KubeStatus(context="internal", server="s", allowed=True, ready=True, permissions={"get nodes": True})
    await fakes.CTX.manager.prepare("master")
    await user.open("/runs")
    user.find("Запустить бенч").click()
    await user.should_see("Пресет")
    user.find("Превью и preflight").click()
    await user.should_see("Preflight:")
    user.find("Полный конфиг: файлы, комментарии, правка").click()
    await user.should_see("exp.yaml — эксперимент fleet")
    await user.should_see("harbor/agent/terminus2.yaml · ревизия fleet")
    await user.should_see("Агент terminus-2 ходит в Polar gateway.")
    await user.should_see("resolved.yaml — итоговый конфиг (--print-config)")
    # клик по ключу в дереве resolved.yaml — откуда пришло значение
    user.find(kind=ui.tree).trigger("update:selected", "system.namespace")
    await user.should_see("ревизия fleet · " + str(fakes.CTX.manager.worktree / CONFIGS_REL / "system" / "cluster.yaml") + ":2")
    await user.should_see("итоговое значение: 'swe-bench-v2-20260308'")
    user.find(kind=ui.tree).trigger("update:selected", "experiment.run_id")
    await user.should_see("override командной строки · командная строка")
    user.find(kind=ui.tree).trigger("update:selected", "experiment.model_name")
    await user.should_see("исходная интерполяция: ${mlsc.served_model_name}")
    user.find("Править в этом прогоне").click()  # первый файл из ревизии — system/cluster.yaml
    await user.should_see("копия system/cluster.yaml в каталоге прогона")
    await user.should_see("ручные правки: exp.yaml")
    run_dir = next(p for p in fakes.CTX.paths.runs.iterdir() if (p / "system" / "cluster.yaml").exists())
    (run_dir / "system" / "cluster.yaml").write_text("# Стенд.\nnamespace: other-ns\n", encoding="utf-8")
    user.find("Превью и preflight").click()
    await user.should_see("ручные правки: system/cluster.yaml")
    user.find("Полный конфиг: файлы, комментарии, правка").click()
    await user.should_see("system/cluster.yaml · правка прогона")
    await user.should_see("Diff ручных правок (1)")
    await user.should_see("+namespace: other-ns")
    # правка прогона → overlay под новым именем, с шапкой о происхождении
    from fleet_console.settings import OVERLAY_DIR

    save = user.find("Сохранить в overlay")
    save.elements = {max(save.elements, key=lambda e: e.id)}  # кнопка последнего открытого диалога, а не стёртого
    save.click()
    await user.should_see("system/cluster-console.yaml сохранён в overlay")
    saved = (OVERLAY_DIR / "system" / "cluster-console.yaml").read_text(encoding="utf-8")
    assert saved.startswith(f"# fleet-console: из system/cluster.yaml ревизии {fakes.SHA_MASTER}") and "namespace: other-ns" in saved
    (OVERLAY_DIR / "system" / "cluster-console.yaml").unlink()


async def test_benches_page_warns_when_overlay_original_drifted(user: User) -> None:
    from fleet_console.connect.kube import KubeStatus
    from fleet_console.settings import OVERLAY_DIR

    fakes.CTX.kube_status = KubeStatus(context="internal", server="s", allowed=True, ready=True, permissions={"get nodes": True})
    await fakes.CTX.manager.prepare("master")
    old_sha = "0" * 40
    mine = OVERLAY_DIR / "harbor" / "agent" / "terminus2_mine.yaml"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text(
        f"# @package _global_\n# fleet-console: из harbor/agent/terminus2.yaml ревизии {old_sha} · правка прогона r1\nharbor_job:\n  agents:\n    - name: terminus-2-mine\n",
        encoding="utf-8",
    )
    fakes.CTX.manager.files_at[(old_sha, "harbor/agent/terminus2.yaml")] = "# @package _global_\nharbor_job:\n  agents:\n    - name: terminus-1\n"
    try:
        await user.open("/runs")
        user.find("Запустить бенч").click()
        await user.should_see("Пресет")
        _select(user, "Агент · harbor/agent").set_value("terminus2_mine")
        user.find("Превью и preflight").click()
        await user.should_see("Правки overlay против ревизии")
        await user.should_see("harbor/agent/terminus2_mine.yaml: оригинал harbor/agent/terminus2.yaml изменился после ревизии 00000000")
        user.find("Полный конфиг: файлы, комментарии, правка").click()
        await user.should_see("harbor/agent/terminus2_mine.yaml · overlay сервиса · оригинал изменился")
        await user.should_see("была → стала (ревизия)")
    finally:
        mine.unlink()


async def test_benches_page_offers_dataset_download(user: User) -> None:
    from fleet_console.connect.kube import KubeStatus

    fakes.CTX.kube_status = KubeStatus(context="internal", server="s", allowed=True, ready=True, permissions={"get nodes": True})
    await fakes.CTX.manager.prepare("master")
    tasks_dir = fakes.DATASETS_ROOT / "cowork" / "tasks"
    hidden = tasks_dir.with_name("tasks.hidden")
    tasks_dir.rename(hidden)  # путь из resolved.yaml заглушки фиксирован: прячем сам каталог задач
    try:
        await user.open("/runs")
        user.find("Запустить бенч").click()
        await user.should_see("Пресет")
        user.find("Превью и preflight").click()
        await user.should_see("можно скачать из S3 по реестру cowork")
        user.find("Скачать из S3").click()
        await user.should_see("Скачать датасет из S3 · cowork")
        await user.should_see("harbor_datasets/cowork/tasks/ → ")
        await user.should_see("· задач: 3")  # лимит задач в форме пуст → первые 3 по имени
        await user.should_see("--include 't1/*'")  # shlex.join берёт glob в кавычки
        user.find(marker="fetch-dataset").click()
        await user.should_see("готово: строк вывода 2")
        assert tasks_dir.is_dir()
        assert any(e.action == "dataset_fetch" and e.result == "ok" for e in fakes.CTX.store.events())
    finally:
        if tasks_dir.exists():
            import shutil

            shutil.rmtree(tasks_dir)
        hidden.rename(tasks_dir)


async def test_local_tasks_import_upload_and_run_form(user: User, tmp_path: Path, monkeypatch, request) -> None:
    from importlib import import_module

    from test_local_datasets import task

    source = tmp_path / "local-source"
    task(source)
    monkeypatch.setattr(import_module("fleet_console.compose.local_datasets"), "secrets_env", lambda *args: {
        "AWS_ACCESS_KEY_ID_gcvmfleet": "test-key", "AWS_SECRET_ACCESS_KEY_gcvmfleet": "test-secret",
    })
    fakes.CTX.runner.handlers.insert(0, (("aws", "s3", "cp"), fakes.Result((), 0, "uploaded")))
    await fakes.CTX.manager.prepare("master")
    await user.open("/runs")
    user.find(marker="local-add").click()
    await user.should_see("Добавить локальные задачи")
    user.find(marker="local-folder").elements.pop().set_value(str(tmp_path / "missing"))
    user.find(marker="local-check").click()
    await user.should_see("Исправьте ошибки")
    assert not user.find(marker="local-import").elements.pop().enabled
    user.find(marker="local-folder").elements.pop().set_value(str(source))
    user.find(marker="local-check").click()
    await user.should_see("Минимальная проверка пройдена")
    user.find(marker="local-name").elements.pop().set_value("Локальный набор")
    user.find(marker="local-import").click()
    await user.should_see("Добавлен набор")
    item = fakes.CTX.local_datasets.all()[0]
    from fleet_console.settings import OVERLAY_DIR
    request.addfinalizer(lambda: (OVERLAY_DIR / "datasets" / f"{item.id}.yaml").unlink(missing_ok=True))
    assert any(r.get("id") == item.id for t in user.find(kind=ui.table).elements for r in t.rows)
    user.find(marker="local-datasets").trigger("rowClick", [None, {"id": item.id}])
    await user.should_see("Пока только локальная копия")
    assert not user.find(marker="local-run").elements.pop().enabled
    next(i for i in user.find(kind=ui.input).elements if i.props.get("label") == "S3-префикс датасетов").set_value("s3://bucket/harbor_datasets")
    user.find(marker="local-upload").click()
    await asyncio.gather(*fakes.CTX.local_datasets.jobs.values())
    await user.should_see("Загружено в S3")
    await user.should_see("Лог отправки")
    user.find("Лог отправки").click()
    assert any("uploaded" in element.content for element in user.find(kind=ui.code).elements)
    user.find(marker="local-run").click()
    await user.should_see("Из локальной папки:")
    selected = _select(user, "Датасет · группа или реестр")
    assert "из локальной папки" in selected.options[selected.value]
    prefix = f"harbor_datasets/local/{item.id}/tasks/"
    listing = ("aws", "s3api", "list-objects-v2", "--bucket", "bucket", "--prefix", prefix)
    fakes.CTX.runner.on(*listing, stdout="[]")
    user.find("Превью и preflight").click()
    await user.should_see("Отсутствуют в S3: one")
    user.find(marker="accept-warnings").elements.pop().set_value(True)
    assert not user.find(marker="start-bench").elements.pop().enabled
    fakes.CTX.runner.handlers.insert(0, (listing, Result((), 0, json.dumps([prefix + "one/task.toml"]))))
    user.find("Превью и preflight").click()
    await user.should_not_see("Отсутствуют в S3: one")
    draft = next(fakes.CTX.paths.runs.glob("*/spec.json"))
    from fleet_console.compose import BenchSpec
    assert BenchSpec.from_json(draft.read_text()).dataset_source["source"] == str(source)
    user.find(marker="accept-warnings").elements.pop().set_value(True)
    user.find(marker="start-bench").click()
    await user.should_see("Запустить прогон?")
    user.find("Да, запустить").click()
    await user.should_see("Заявка принята")
    await asyncio.gather(*fakes.CTX.benches.tasks.values())
    run = next(r for r in fakes.CTX.benches.runs() if r.id == draft.parent.name)
    assert run.status == "done"
    await user.open("/runs")
    run_table = user.find(kind=ui.table)
    run_table.elements = {next(t for t in run_table.elements if any(r.get("id") == run.id for r in t.rows))}
    run_table.trigger("rowClick", [None, {"id": run.id}])
    await user.should_see("Данные из локальной папки:")


async def test_benches_form_offers_registry_benches_and_fills_path(user: User) -> None:
    from fleet_console.connect.kube import KubeStatus

    fakes.CTX.kube_status = KubeStatus(context="internal", server="s", allowed=True, ready=True, permissions={"get nodes": True})
    await fakes.CTX.manager.prepare("master")
    registry = fakes.CTX.manager.worktree / CONFIGS_REL / "datasets"
    extra = {
        registry / "deep_swe.yaml": "benchmark: deep_swe_v1\nsource:\n  tasks_root: deep_swe/tasks\nruntime:\n  task_s3_base: deep_swe/tasks\n",
        registry / "cowork_bench_ua_external_260902.yaml": (  # запись под пресет qwen36-cowork-smoke репозитория
            "benchmark: cowork_bench_ua_external_260902\nsource:\n  tasks_root: cowork_bench_ua_external_260902/tasks\nruntime:\n  task_s3_base: x\n"
        ),
    }
    for path, text in extra.items():
        path.write_text(text, encoding="utf-8")
    try:
        await user.open("/runs")
        user.find("Запустить бенч").click()
        await user.should_see("Пресет")
        dataset = _select(user, "Датасет · группа или реестр")
        # cowork реестра закрыт одноимённой группой — группа важнее; бенчи только из реестра идут после групп
        assert list(dataset.options) == ["cowork", "registry:cowork_bench_ua_external_260902", "registry:deep_swe", "__custom__"]
        assert dataset.options["registry:deep_swe"] == "deep_swe · из реестра" and dataset.value == "cowork"
        dataset.set_value("registry:deep_swe")
        await asyncio.sleep(0.05)
        inputs = {i.props.get("label"): i for i in user.find(kind=ui.input).elements}
        assert inputs["путь датасета относительно FLEET_DATASETS_ROOT"].value == "deep_swe/tasks" and inputs["benchmark"].value == "deep-swe-v1"
        assert _select(user, "реестр datasets").value == "deep_swe"
        user.find("Превью и preflight").click()
        await user.should_see("Что и где будет запущено")
        exp = next(fakes.CTX.paths.runs.glob("glm-deep-swe-v1-*/exp.yaml")).read_text()
        assert "harbor/dataset:" not in exp and "${system.datasets_root}/deep_swe/tasks" in exp and "benchmark: deep-swe-v1" in exp
        assert "workflow_prefix: h-deep-swe-v1" in exp
        # пресет с путём из реестра выбирает пункт реестра, но benchmark берёт свой
        _select(user, "Пресет").set_value("qwen36-cowork-smoke")
        await asyncio.sleep(0.05)
        assert dataset.value == "registry:cowork_bench_ua_external_260902" and inputs["benchmark"].value == "cowork"
        assert inputs["путь датасета относительно FLEET_DATASETS_ROOT"].value == "cowork_bench_ua_external_260902/tasks"
        assert _select(user, "реестр datasets").value == "cowork_bench_ua_external_260902"
    finally:
        for path in extra:
            path.unlink()


async def test_queued_run_card_has_no_live_actions(user: User) -> None:
    from fleet_console.store import BenchRun

    ctx = fakes.CTX
    ctx.store.create_bench(BenchRun(id="waiting", status="queued", created_at="2026-11-11", owner_id="local"))
    await user.open("/runs")
    table = next(t for t in user.find(kind=ui.table).elements if any(r.get("id") == "waiting" for r in t.rows))
    row = next(r for r in table.rows if r.get("id") == "waiting")
    user.find(kind=ui.table).elements = {table}
    user.find(kind=ui.table).trigger("rowClick", [None, row])
    await user.should_see("Ожидает свободной квоты. Ресурсы ещё не выделены.")
    await user.should_see("Отменить заявку")
    await user.should_not_see("Задачи прогона")


async def test_bench_status_and_open_card_follow_mlsc_and_stage_changes(user: User) -> None:
    ctx = fakes.CTX
    now = fakes.NOW.isoformat()
    conf = json.loads(cluster_samples.CONFIGURATION)
    conf["application_version_states"]["41"] = {"planned": 1, "current": 0}
    conf["application_versions"]["40"] = {"last_deploy_error": "PriorityNotApplicable"}
    del conf["application_version_states"]["40"]  # MLSC не возвращает состояние погашенной версии
    ctx.runner.handlers.insert(0, (("kubectl", "exec", "-i"), lambda argv: Result(argv, 0, json.dumps(conf))))
    ctx.store.create_bench(BenchRun("waiting-model", "running", now, stage="check", version_id=41))
    ctx.store.create_bench(BenchRun("stopped-model", "stopped", now, version_id=40))
    ctx.store.create_bench(BenchRun("failed-run", "failed", now, stage="materialize", error="Задачи датасета не найдены"))
    stage_id = ctx.store.start_bench_stage("waiting-model", "check", "fleet check")
    await ctx.observer.refresh()

    await user.open("/runs")
    run_list = user.find(marker="bench-runs").elements.pop()
    rows = {row["id"]: row for row in run_list.rows}
    assert rows["waiting-model"]["status"] == "ждёт готовности модели"
    assert rows["waiting-model"]["replicas"] == "Реплики готовы: 0/1"
    assert rows["stopped-model"]["errors"] == ["MLSC · последняя ошибка: PriorityNotApplicable"]
    assert rows["failed-run"]["status_color"] == "text-negative"
    assert rows["failed-run"]["errors"] == ["Задачи датасета не найдены"]

    user.find(marker="bench-runs").trigger("rowClick", [None, rows["waiting-model"]])
    await user.should_see("Реплики готовы: 0/1")
    stage = next(e for e in user.find("подъём модели: check_llm_works · идёт").elements if isinstance(e, ui.expansion))
    stage.open()
    dialog = user.find(kind=ui.dialog).elements.pop()
    assert dialog.value
    user.find("Остановить прогон").click()
    await user.should_see("Остановить прогон waiting-model?")

    conf["application_version_states"]["41"]["current"] = 1
    ctx.store.finish_bench_stage(stage_id, 0)
    ctx.store.update_bench("waiting-model", stage="execute", counters_json='{"running": 2, "failed": 1}')
    await ctx.observer.refresh()
    await user.should_see("Реплики готовы: 1/1", retries=40)
    await user.should_see("Сбои workflows: 1")
    assert stage.value and stage.text.startswith("подъём модели: check_llm_works · код 0")
    assert dialog.value and dialog in user.find(kind=ui.dialog).elements
    await user.should_see("Остановить прогон waiting-model?")
    row = next(row for row in run_list.rows if row["id"] == "waiting-model")
    assert "исполнение" in row["status"] and row["replicas"] == "Реплики готовы: 1/1"


async def test_closed_run_opens_failed_retry_confirmation(user: User):
    ctx = fakes.CTX
    run = BenchRun("retry-demo", "queued", fakes.NOW.isoformat(), version_id=1677,
                   preview_json=json.dumps({"s3": "s3://b/ns/GLM/retry-demo"}))
    ctx.store.enqueue(run, 8, run.id)
    ctx.store.update_bench(run.id, status="stopped")
    ctx.store.release(run.id)
    await user.open("/runs/retry-demo")
    await user.should_see("Повторить неудачные попытки")
    user.find("Повторить неудачные попытки").click()
    await user.should_see("последних попыток с FAILED: 1")
    await user.should_see("резерв 8 GPU")
    user.find("Отмена").click()
    assert ctx.store.request(run.id)["state"] == "released"
