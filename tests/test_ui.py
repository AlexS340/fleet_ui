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
    await user.should_see("Argo")
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
