"""Main-файл для симуляции NiceGUI: приложение на фейковом контексте."""

from nicegui import ui

import fakes
from fleet_console.app import create_app

fakes.CTX = fakes.make_context()
create_app(fakes.CTX)
ui.run(reload=False, show=False)
