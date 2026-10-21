import asyncio

from nicegui import ui
from nicegui.testing import User
import pytest

from fleet_console.fleetref.profile import CONFIGS_REL
import fakes

pytestmark = pytest.mark.nicegui_main_file("tests/ui_main.py")


def _select(user: User, label: str) -> ui.select:
    return next(s for s in user.find(kind=ui.select).elements if s.props.get("label") == label)


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
    await user.should_see("ssh -N fleetbastion")
    user.find("Перезапустить").click()
    await user.should_see("бастион: перезапуск")
    assert fakes.CTX.supervisor.restarted[-1] == "bastion"


async def test_settings_page_saves(user: User) -> None:
    await user.open("/settings")
    await user.should_see("Настройки")
    user.find("Сохранить").click()
    await user.should_see("Сохранено")
    assert fakes.CTX.paths.settings_file.exists()


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
    finally:
        await fakes.CTX.guard.update({})


async def test_models_page_sessions(user: User) -> None:
    await user.open("/models")
    await user.should_see("Сессии моделей")
    user.find("Поднять модель").click()
    await user.should_see("Сначала подготовьте совместимую ревизию")
    user.find("Закрыть").click()
    await fakes.CTX.manager.prepare("master")
    user.find("Поднять модель").click()
    await user.should_see("Рецепт mlsc/model")
    await user.should_see("в рецепте: served_model_name GLM")
    user.find("Запустить подъём").click()
    await user.should_see("Занять GPU под модель?")
    await user.should_see("стадии: preflight → mlsc.create_job")
    user.find("Да, поднять").click()
    await user.should_see("подъём начат")
    await asyncio.gather(*fakes.CTX.sessions.tasks.values())
    session = fakes.CTX.sessions.sessions()[0]
    assert session.status == "ready" and session.version_id == 777 and session.id.startswith("console-glm-")
    assert [s.stage for s in fakes.CTX.sessions.stages(session.id)] == ["preflight", "create_job", "start_llm", "check"]
    assert "session:" + session.id in fakes.CTX.sleep_reasons()
    await fakes.CTX.sessions.stop(session.id)
    assert fakes.CTX.sessions.get(session.id).status == "stopped"


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
    await user.should_see("GPU: 1 × 8 = 8 · лимит агентов 1 × 2 = 2")
    user.find("Превью и preflight").click()
    await user.should_see("Что и где будет запущено")
    preview = {r["k"]: r["v"] for t in user.find(kind=ui.table).elements for r in t.rows if "k" in r}
    assert preview["GPU"] == "1 × a100plus.8gpu.80vG.96C.1456G = 8 GPU" and preview["Параллелизм"] == "лимит 2 = 1 реплик × 2 агентов"
    assert preview["Модель"].startswith("GLM · образ fleet-vllm:0.24.0 · очередь gigacode · поднимет сервис")
    await user.should_see("Preflight: ✗ 0 · ⚠ 2")  # квота GPU и terminus-2 на Harbor 0.22
    await user.should_see("tasks → cowork (fleet)")
    start = user.find(marker="start-bench").elements.pop()
    assert not start.enabled  # предупреждения не приняты
    user.find(marker="accept-warnings").elements.pop().set_value(True)
    assert start.enabled
    user.find(marker="start-bench").click()
    await user.should_see("Запустить прогон?")
    await user.should_see("стадии: preflight → материализация → create_job → start_llm → check → upload → launch → исполнение → stop_llm → замер")
    user.find("Да, запустить").click()
    await user.should_see("начат")
    await asyncio.gather(*fakes.CTX.benches.tasks.values())
    run = fakes.CTX.benches.runs()[0]
    assert run.status == "done" and run.version_id == 777 and run.task_limit == 3 and run.agents_per_replica == 2 and run.id.startswith("glm-cowork-")
    assert [s.stage for s in fakes.CTX.benches.stages(run.id)] == ["preflight", "materialize", "create_job", "start_llm", "check", "upload", "launch", "execute", "stop_llm", "measure"]
    assert fakes.CTX.store.get_run(run.id) is not None and run.preview["gpu_per_instance"] == 8 and run.preview["tasks_total"] is None
    exp = (fakes.CTX.paths.runs / run.id / "exp.yaml").read_text()
    assert "harbor/dataset: cowork" in exp and "task_limit: 3" in exp and (fakes.CTX.paths.runs / run.id / "resolved.yaml").exists()
    # карточка прогона по клику на строку
    await user.open("/runs")
    await user.should_see("клик по строке — карточка прогона")
    table = next(iter(user.find(kind=ui.table).elements))
    assert table.rows[0]["tasks"] == "0 / 0 / 3" and table.rows[0]["status"] == "готов"
    user.find(kind=ui.table).trigger("rowClick", [None, {"id": run.id}])
    await user.should_see("Что и где запущено")
    await user.should_see("подготовка: --print-config · код 0")
    await user.should_see("замер (Benchboard) · код 0")
    await user.should_see("Замер заново")
    await user.should_see("Журнал")


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
