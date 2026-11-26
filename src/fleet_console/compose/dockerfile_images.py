"""Чтение базовых образов verifier без запуска сборки."""

from collections.abc import Iterator
from pathlib import Path
from string import Template

import dockerfile


def base_images(path: Path) -> Iterator[tuple[int, str]]:
    """Строка FROM и образ. Неразрешённые переменные остаются в ссылке."""
    try:
        commands = dockerfile.parse_string(path.read_text(encoding="utf-8-sig"))
    except (dockerfile.GoParseError, UnicodeError) as exc:
        raise ValueError(f"tests/Dockerfile: {exc}") from exc
    args: dict[str, str] = {}
    stages: set[str] = set()
    in_stage = False
    for command in commands:
        if command.cmd.lower() == "arg" and not in_stage:
            for arg in command.value:
                name, equals, default = arg.partition("=")
                if equals:
                    if len(default) >= 2 and default[0] == default[-1] and default[0] in "\"'":
                        default = default[1:-1]
                    value = Template(default).safe_substitute(args)
                    # Ограничиваем рост вложенных ARG в файле, предоставленном пользователем.
                    if len(value) <= 4096:
                        args[name] = value
                    else:
                        args.pop(name, None)
        if command.cmd.lower() != "from":
            continue
        in_stage = True
        fields = command.value
        if len(fields) != 1 and not (len(fields) == 3 and fields[1].lower() == "as"):
            raise ValueError(f"tests/Dockerfile:{command.start_line}: некорректная инструкция FROM")
        image = Template(fields[0]).safe_substitute(args)
        if image != "scratch" and image.lower() not in stages:
            yield command.start_line, image
        if len(fields) == 3:
            stages.add(fields[2].lower())
    if not in_stage:
        raise ValueError("tests/Dockerfile: нет инструкции FROM")
