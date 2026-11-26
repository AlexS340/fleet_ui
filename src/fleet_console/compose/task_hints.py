"""Статические подсказки автору Harbor-задачи. Код из папки не выполняется."""

import re
from pathlib import Path

_ANSWER_NAME = re.compile(r"(?:^|[^a-z0-9])(?:oracle|golden|solutions?|answers?)(?:[^a-z0-9]|$)", re.IGNORECASE)
_REWARD_NAME = rb"reward\.(?:txt|json)(?![\w.-])"
_REWARD_FILE = re.compile(rb"(?<![\w/.-])(?:/logs/verifier/)?" + _REWARD_NAME)
_SHELL_OUTPUT = re.compile(rb"(?:>>?|\btee\s+(?:-a\s+)?)\s*['\"]?/logs/verifier/" + _REWARD_NAME)
_REWARD_VARIABLE = re.compile(rb"""\b([A-Za-z_]\w*)=['"]?/logs/verifier/""" + _REWARD_NAME)
_OUTPUT_VARIABLE = re.compile(rb"""(?:>>?|\btee\s+)\s*['"]?\$\{?([A-Za-z_]\w*)""")
_FILE_WRITE = re.compile(rb"\.write_(?:text|bytes)\s*\(|['\"](?:w|a)[bt+]?['\"]|\bwriteFile(?:Sync)?\s*\(")
_SCRIPT_NAME = re.compile(rb"[\w.-]+\.(?:py|sh|bash|js|ts|mjs|rb|pl)\b")
_ENTRY = Path("tests/test.sh")
_ORACLE = Path("solution/solve.sh")


def _verifier_hints(task: Path, scripts: dict[bytes, list[Path]]) -> list[str]:
    hints = []
    pending = {_ENTRY}
    visited = set()
    reward = False
    while pending:
        rel = pending.pop()
        visited.add(rel)
        references = set()
        reward_variables = set()
        output_variables = set()
        crlf = shell_output = reward_file = file_write = False
        with (task / rel).open("rb") as stream:
            for index, line in enumerate(iter(lambda: stream.readline(65536), b"")):
                if rel == _ENTRY and index == 0 and not line.startswith(b"#!"):
                    hints.append(f"{task.name}/{rel}: добавьте shebang с интерпретатором в первую строку.")
                crlf |= line.endswith(b"\r\n")
                if line.lstrip().startswith(b"#"):
                    continue
                shell_output |= bool(_SHELL_OUTPUT.search(line))
                reward_file |= bool(_REWARD_FILE.search(line))
                file_write |= bool(_FILE_WRITE.search(line))
                reward_variables.update(_REWARD_VARIABLE.findall(line))
                output_variables.update(_OUTPUT_VARIABLE.findall(line))
                references.update(_SCRIPT_NAME.findall(line))
        if rel == _ENTRY and crlf:
            hints.append(f"{task.name}/{rel}: замените окончания строк CRLF на LF.")
        reward |= shell_output or (reward_file and file_write) or bool(reward_variables & output_variables)
        for name in references:
            pending.update(path for path in scripts.get(name, ()) if path not in visited)
    if not reward:
        hints.append(f"{task.name}/{_ENTRY}: проверьте verifier — в скрипте и его помощниках не видна запись "
                     "/logs/verifier/reward.txt или /logs/verifier/reward.json.")
    return hints


def author_hints(task: Path, entries: list[Path]) -> list[str]:
    hints = []
    scripts: dict[bytes, list[Path]] = {}
    has_oracle = False
    for path in entries:
        rel = path.relative_to(task)
        if rel.parts[0] == "environment" and _ANSWER_NAME.search(rel.name):
            hints.append(f"{task.name}/{rel}: проверьте, должен ли агент видеть этот возможный эталонный ответ.")
        if rel == _ORACLE:
            has_oracle = not path.is_symlink() and path.is_file()
        if rel.parts[0] == "tests" and not path.is_symlink() and path.is_file():
            scripts.setdefault(path.name.encode(), []).append(rel)
    if not has_oracle:
        hints.append(f"{task.name}/{_ORACLE}: для oracle-проверки добавьте эталонное решение.")
    if _ENTRY in scripts.get(b"test.sh", ()):
        hints.extend(_verifier_hints(task, scripts))
    return hints
