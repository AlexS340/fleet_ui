import asyncio

from nicegui import ui
from nicegui.testing import User
import pytest

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
    try:
        await user.open("/cluster")
        user.find("Обновить").click()
        await user.should_see("скрыто завершённых прогонов: 2")
        tables = {t.columns[-1]["name"]: t for t in user.find(kind=ui.table).elements}
        assert tables["argo"].rows[0]["own"] == "да"
        assert [m["own"] for m in tables["gpus"].rows] == ["да", "—"]  # версия 41 своя, 42 чужая
    finally:
        fakes.CTX.store.forget_run("run-a")


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
