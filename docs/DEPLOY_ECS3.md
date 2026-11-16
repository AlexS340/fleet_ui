# Fleet Console на ecs3

Локальный запуск и запуск через systemd используют один checkout и одну папку `config/`. Выберите один режим для каждого checkout.

## Общая настройка

1. Клонируйте репозиторий на машину, где будет работать Console. На ecs3 предполагается путь `~/src/fleet_ui`. Если у ecs3 нет доступа к GitHub, сделайте `git bundle create fleet-console.bundle phase-6` на Mac, передайте bundle через `scp` и укажите его путь вместо URL в команде клонирования.

   ```bash
   mkdir -p "$HOME/src"
   git clone --branch phase-6 git@github.com:AlexS340/fleet_ui.git "$HOME/src/fleet_ui"
   cd "$HOME/src/fleet_ui"
   umask 077
   for file in settings.yaml kubeconfig ssh_config aws-credentials secrets.env; do
     cp -n "config/$file.example" "config/$file"
     chmod 600 "config/$file"
   done
   ```

2. Заполните файлы в `config/`. Для существующих пользовательских настроек можно пропустить копирование соответствующего шаблона.

   | Файл | Что заполнить |
   | --- | --- |
   | `settings.yaml` | Шаблон задаёт UI на `18082` и SOCKS на `11080`. Проверьте контекст, namespace и профили S3. |
   | `kubeconfig` | Положите выданный kubeconfig с правами для текущего стенда. Укажите сервер `https://127.0.0.1:18443`, сохранив настройки TLS и аутентификации. |
   | `ssh_config` | Заполните хост и пользователя бастиона, абсолютные пути к ключам и `known_hosts`. Положите эти файлы в `config/`, ключам задайте `0600`. На ecs3 пробросы `18443` и `11080` отделены от уже занятых `8443` и `1080`. |
   | `aws-credentials` | Заполните профиль для записи `gcvmfleet` и профиль для чтения, указанный в настройках. |
   | `secrets.env` | Заполните `MLSC_TOKEN`. Для стенда задайте исходные учётные данные в `FLEET_CONSOLE_TEST_USERS`, а в settings — `auth.mode: test` и `auth.admins`. Дальше меняйте пароли, роли и доступ в `/admin`: эти изменения сохраняются в SQLite и вступают в силу без перезапуска. |

   При включённом входе задайте `results.private_s3_root: s3://bucket/prefix` для закрытого бакета. Чтобы сохранить путь из рецепта Fleet, как при локальном запуске, оставьте `private_s3_root` пустым и разрешите общий бакет в настройках Console («Квоты и хранение результатов») или в `config/settings.yaml`:

   ```yaml
   results:
     private_s3_root: ""
     allow_shared_bucket: true
   ```

   Вход и права на прогоны в Console сохраняются. Артефакты можно читать напрямую через S3 при наличии прав на общий бакет.

   Console предпочитает файлы проекта. Если отдельного файла нет, действуют предыдущие пути и переменные окружения. При наличии `config/settings.yaml` SQLite, зеркало Fleet и логи по умолчанию создаются в `.fleet-console/` внутри checkout. Git отслеживает только шаблоны из `config/`.

3. С ecs3 проверьте GitVerse с той же SSH-конфигурацией, которую будет использовать Console:

   ```bash
   GIT_SSH_COMMAND='ssh -F config/ssh_config' git ls-remote git@gitverse.ru:gigacode-ai/fleet.git HEAD
   ```

   При проверке 16 ноября GitVerse закрывал SSH до аутентификации на портах 22 и 2222, а индекс пакетов отвечал. Для зеркала Fleet и его git-зависимостей на ecs3 нужен рабочий SSH-маршрут. При использовании пользовательского SSH-конфига запустите проверку обычным `git ls-remote`.

## А. Развернуть как сервис на ecs3

1. Скопируйте шаблон unit. Если checkout лежит по другому пути, поправьте `WorkingDirectory`. Шаблон использует обнаруженный на ecs3 `uv` из `/cache/miniconda/bin/`.

   ```bash
   cp -n config/fleet-console.service.example config/fleet-console.service
   loginctl enable-linger "$(id -un)"
   systemctl --user link "$PWD/config/fleet-console.service"
   systemctl --user enable --now fleet-console.service
   ```

2. Проверьте запуск через `systemctl --user status fleet-console.service`. Для просмотра лога используйте `journalctl --user -u fleet-console.service -f`. Если хост требует права администратора для `enable-linger`, попросите его включить linger для вашей учётной записи.

## Б. Запустить локально в терминале

1. Из корня checkout выполните `uv run fleet-console`. uv подготовит окружение и запустит Console.
2. Откройте `http://127.0.0.1:18082`, если запускали на своей машине. Для запуска в терминале на ecs3 используйте тот же SSH-туннель, что и для сервиса. Остановка в терминале через `Ctrl+C`.

## Открыть UI на ecs3 и проверить

С Mac пробросьте UI и откройте `http://127.0.0.1:18082`:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18082:127.0.0.1:18082 ecs3
```

В Console проверьте контекст и состояние MLSC, Argo и submitter, затем подготовьте нужный SHA Fleet и пройдите preflight. Живой smoke с GPU согласуется отдельно.
