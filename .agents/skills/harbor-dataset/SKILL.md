---
name: harbor-dataset
description: Подготовить папку с Linux-задачами Harbor для импорта в Fleet Console и проверить её структуру, конфигурацию и verifier до создания снимка.
---

# Подготовка датасета Harbor для Fleet Console

Передайте автору прогона папку набора, где каждый непосредственный подкаталог — самостоятельная задача. Имя каталога служит именем задачи по умолчанию. Используйте разные имена. Console также принимает одну папку с `task.toml`. Если задачи сгруппированы по языкам, выбирайте для импорта конкретную группу, например `dataset/ru`. Внутри задачи используйте обычные файлы и каталоги, замените символические ссылки их содержимым.

## Пример из одной задачи

Выполните блок в пустом рабочем каталоге в POSIX-совместимой оболочке. Получится задача на сложение с однозначным результатом и эталонным решением для oracle.

```sh
task=dataset/add-two
mkdir -p "$task/environment" "$task/tests" "$task/solution"

cat > "$task/instruction.md" <<'EOF'
Сложите 19 и 23. Запишите сумму десятичными цифрами в /app/sum.txt.
После числа должен быть один перевод строки.
EOF

cat > "$task/task.toml" <<'EOF'
[environment]
docker_image = "registry.mlrnd.ru/docker-mlrnd/sheet-taskgen/harbor-sheet-taskgen@sha256:394369564ed75fbf10714514531826e42989d1bb5020c48975ce13871b2103e1"

[verifier]
environment_mode = "shared"
EOF

cat > "$task/environment/Dockerfile" <<'EOF'
FROM registry.mlrnd.ru/docker-mlrnd/sheet-taskgen/harbor-sheet-taskgen@sha256:394369564ed75fbf10714514531826e42989d1bb5020c48975ce13871b2103e1
EOF

cat > "$task/solution/solve.sh" <<'EOF'
#!/bin/sh
set -eu
printf '%s\n' "$((19 + 23))" > /app/sum.txt
EOF

cat > "$task/tests/test.sh" <<'EOF'
#!/bin/sh
set -eu
mkdir -p /logs/verifier
reward=0
if [ -f /app/sum.txt ] && printf '42\n' | cmp -s - /app/sum.txt; then
    reward=1
fi
printf '%s\n' "$reward" > /logs/verifier/reward.txt
EOF

chmod +x "$task/solution/solve.sh" "$task/tests/test.sh"
```

`instruction.md` — текст для агента. Опишите входные данные, ожидаемый результат, точный путь и критерий приёмки. В примере входные числа даны в условии, каталог `/app` уже есть в образе. Для задач с входными файлами включите их в основной образ по путям из условия. Храните эталон в `solution/`, а проверочные данные в `tests/`, за пределами основного образа. Скрипт `solution/solve.sh` нужен для прогона oracle и необязателен для импорта.

## Образы и verifier

- **Среда агента.** Console требует каталог `environment/` и непустую ссылку `[environment].docker_image` на заранее собранный образ. Локальные изменения Dockerfile и входных файлов попадут в задачу при выполнении только после пересборки образа и обновления ссылки. Для кластера берите доступные образы из `registry.mlrnd.ru`, в том числе в `FROM` и для verifier. Ссылка с digest закрепляет содержимое. Тег может измениться.
- **Общая среда.** При `environment_mode = "shared"` Harbor запускает проверку в среде агента с доступом к созданным файлам. Оставьте таблицу `[verifier.environment]` за пределами такого конфига. Console ожидает непустой `tests/test.sh`; используйте shebang для установленной оболочки и переводы строк LF.
- **Отдельная среда.** При `environment_mode = "separate"` у verifier своя файловая система. Объявите выходные файлы агента в корневом поле TOML `artifacts` и проверьте путь, по которому verifier получает каждый артефакт. Для готового `[verifier.environment].docker_image` включите `/tests/test.sh` и его зависимости в сам образ. Для сборки из `tests/Dockerfile` включите в него шаг копирования тестов. Сохраните `tests/test.sh` и в каталоге задачи для проверки Console.
- **Оценка.** Verifier записывает число в `/logs/verifier/reward.txt` либо объект численных метрик, например `{"accuracy": 1}`, в `/logs/verifier/reward.json`. Пишите один формат. Вывода в stdout и кода завершения недостаточно. В примере точное совпадение с ожидаемыми байтами даёт 1, отсутствующий или неверный ответ даёт 0. В обоих случаях проверка заканчивается с записанной оценкой.

## Проверка перед импортом

1. На странице прогонов Console выберите «Добавить из папки», укажите каталог набора на машине сервиса и нажмите «Проверить папку». Для примера ожидайте «Задач: 1» и «Минимальная проверка пройдена». Исправьте ошибки до создания снимка. Ответ реестра 404 для манифеста блокирует импорт. При ошибке доступа предупреждение означает, что доступность образа ещё предстоит подтвердить.
2. Для автономной проверки структуры укажите абсолютный путь к набору в переменной `DATASET`. Пример команды из корня подготовленного проекта Fleet Console:

   ```sh
   .venv/bin/python - "$DATASET" <<'PY'
   import sys
   from pathlib import Path
   from fleet_console.compose.local_datasets import validate_folder

   report = validate_folder(Path(sys.argv[1]))
   print("Задачи:", [task.name for task in report.tasks])
   for message in report.errors + report.warnings:
       print(message)
   raise SystemExit(0 if report.ok else 1)
   PY
   ```

3. Проверьте тот же каталог установленным Harbor **выбранной ревизии Fleet**. Укажите в `WORKTREE` абсолютный путь к её подготовленному worktree. Команда ниже рассчитана на Harbor 0.22.0 и читает конфиг и файлы каждой задачи без запуска контейнеров.

   ```sh
   "$WORKTREE/.venv/bin/python" -B - "$DATASET" <<'PY'
   import sys
   from pathlib import Path
   from harbor.models.task.task import Task

   root = Path(sys.argv[1])
   tasks = [root] if (root / "task.toml").is_file() else sorted(
       p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
   )
   if not tasks:
       raise SystemExit("Пустой набор")
   for path in tasks:
       print(path, flush=True)
       task = Task(path)
       if not Task.is_valid_dir(path):
           raise SystemExit(f"Harbor отклонил {path}")
       print("OK", task.name)
   PY
   ```

Ожидаемый результат последней команды для примера — `OK add-two`. Проверка структуры, TOML и путей выполняется локально. Чтобы подтвердить исполнение, нужен прогон с этим образом и чтение файла reward после verifier.
