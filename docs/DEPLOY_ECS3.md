# Fleet Console на ecs3

Один экземпляр Console под `systemd --user`, отдельные данные в `~/.fleet-console-ecs3` и доступ с Mac через SSH. Это серверный режим для личного использования или закрытой проверки. Гейт Ф7 с заменой Kubernetes pod остаётся отдельным.

Проверка ecs3 2026-11-16: Linux x86-64, uv `/cache/miniconda/bin/uv`, kubectl и AWS CLI установлены, по именам доступны профили `gcvmfleet` и `gcvmfleet_ro`. Пользовательский systemd работает, `Linger=no`. Порты 8080, 8443 и 1080 заняты, 18082, 18443 и 11080 свободны на момент проверки. Прямой HTTPS к индексу пакетов Fleet вернул 200. GitHub по SSH не прошёл проверку ключа хоста, а GitVerse закрыл SSH-соединение до аутентификации на 22 и 2222. На ecs3 также пока нет kube context `internal` и SSH-алиаса бастиона. Доступ GitVerse нужен для зеркала и закреплённых git-зависимостей Fleet. Разобраться с маршрутом до GitVerse стоит до запуска сервиса.

## 1. Положить код на сервер

Вариант с доступом ecs3 к GitHub по SSH, после проверки ключа хоста и настройки ключа для чтения репозитория:

```bash
mkdir -p "$HOME/src"
git clone --branch phase-6 git@github.com:AlexS340/fleet_ui.git "$HOME/src/fleet_ui"
```

Если доступ к GitHub с ecs3 не настроен, на Mac из checkout Fleet Console можно собрать bundle и передать его по уже работающему SSH:

```bash
umask 077
git bundle create "$HOME/fleet-console-phase-6.bundle" phase-6
scp "$HOME/fleet-console-phase-6.bundle" ecs3:fleet-console-phase-6.bundle
```

Затем на ecs3:

```bash
mkdir -p "$HOME/src"
git clone --branch phase-6 "$HOME/fleet-console-phase-6.bundle" "$HOME/src/fleet_ui"
```

После любого варианта на ecs3:

```bash
cd "$HOME/src/fleet_ui"
git rev-parse HEAD
/cache/miniconda/bin/uv sync --locked --python 3.12 --no-dev
install -d -m 700 "$HOME/.fleet-console-ecs3" "$HOME/.fleet-console-ecs3/overlay" "$HOME/.fleet-console-ecs3/datasets"
cp -a overlay/configs/. "$HOME/.fleet-console-ecs3/overlay/"
```

Сверить SHA с опубликованной веткой. Bundle переносит Console. Для Fleet отдельно проверить с ecs3 `git ls-remote git@gitverse.ru:gigacode-ai/fleet.git HEAD` по рабочему настроенному SSH-маршруту и доступ к GitVerse для git-зависимостей. Console сама создаёт зеркало и worktree закреплённой ревизии.

## 2. Подключить кластер и S3

Выдать на ecs3 отдельный kubeconfig с контекстом `internal` и разрешёнными правами. Сохранить его в `~/.fleet-console-ecs3/kubeconfig` с правами `0600` и адресом API `https://127.0.0.1:18443`. Сохранить параметры TLS и аутентификации исходного kubeconfig.

На ecs3 в `~/.ssh/config` добавить алиас `gigacode-bastion-ecs3`. Параметры самого бастиона и удалённую цель API взять из [рабочей схемы подключения](ARCHITECTURE.md#подключение-к-кластеру). Локальные привязки для этого экземпляра:

```sshconfig
  LocalForward 127.0.0.1:18443 192.168.100.17:5443
  DynamicForward 127.0.0.1:11080
```

Проверить, что выделенный ключ на ecs3 работает с `BatchMode=yes` и доступен пользовательскому systemd без SSH-агента из интерактивной сессии. Рабочий туннель поднимет сама Console.

В `~/.fleet-console-ecs3/settings.yaml` задать:

```yaml
ui_port: 18082
allowed_contexts: [internal]
bastion:
  ssh_host: gigacode-bastion-ecs3
  socks_proxy: socks5h://127.0.0.1:11080
s3_read_profile: gcvmfleet_ro
s3_write_profile: gcvmfleet
auth:
  mode: local
```

Положить `MLSC_TOKEN` в `~/.fleet-console-ecs3/secrets.env` с правами `0600`. S3-профили берутся из `~/.aws/credentials`. Наличие профиля подтверждает только имя. Отдельно сверить разрешение на чтение датасетов, запись результатов и чтение этих результатов профилем для просмотра. Свой корень датасетов передаётся через переменную в unit.

`auth.mode: local` подходит, когда локальным пользователям ecs3 можно доверить доступ к Console. На общем хосте включить `auth.mode: test` для закрытого стенда: задать учётные записи в `~/.fleet-console-ecs3/test-users.env` с правами `0600` (строка вида `FLEET_CONSOLE_TEST_USERS='{"operator":"<пароль>"}'`), внести ID оператора в `auth.admins` и указать `results.private_s3_root` в отдельном закрытом бакете. В этом режиме вход через `/test-login` обязателен. Для общего адреса нужен инфраструктурный вход из Ф8.

## 3. Запустить пользовательский unit

Сохранить как `~/.config/systemd/user/fleet-console.service`:

```ini
[Unit]
Description=Fleet Console on ecs3

[Service]
WorkingDirectory=%h/src/fleet_ui
Environment=PATH=/cache/miniconda/bin:/usr/local/bin:/usr/bin:/bin
Environment=FLEET_CONSOLE_HOME=%h/.fleet-console-ecs3
Environment=FLEET_CONSOLE_OVERLAY=%h/.fleet-console-ecs3/overlay
Environment=FLEET_DATASETS_ROOT=%h/.fleet-console-ecs3/datasets
Environment=KUBECONFIG=%h/.fleet-console-ecs3/kubeconfig
EnvironmentFile=-%h/.fleet-console-ecs3/test-users.env
ExecStart=/cache/miniconda/bin/uv run --no-sync fleet-console
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
```

Для работы после выхода из SSH включить linger для своего пользователя через администратора ecs3, если для команды требуются права. Затем запустить один экземпляр:

```bash
loginctl enable-linger "$(id -un)"
systemctl --user daemon-reload
systemctl --user enable --now fleet-console.service
systemctl --user status fleet-console.service
journalctl --user -u fleet-console.service -n 80 --no-pager
```

## 4. Открыть UI и проверить

На Mac:

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -L 127.0.0.1:18082:127.0.0.1:18082 ecs3
```

Открыть `http://127.0.0.1:18082`. На ecs3 проверить `KUBECONFIG="$HOME/.fleet-console-ecs3/kubeconfig" kubectl --context internal get --raw /readyz`. В UI проверить ожидаемый контекст, чтение MLSC, Argo и submitter, подготовить выбранный SHA Fleet и пройти preflight. Потом переподключиться к ecs3 и убедиться, что Console по-прежнему активна. Эти проверки проходят без выделения GPU.

Для живого smoke заранее согласовать точный пресет, SHA, число задач и GPU. После него сверить замер и освобождение ресурсов в MLSC. Если позже переносить существующую историю с Mac, сначала завершить активные прогоны и выключить старый экземпляр. Журнал и каталоги прогонов содержат абсолютные пути, поэтому перенос истории требует отдельной проверки этих путей на ecs3.

Для планового обновления дождаться завершения активных прогонов, остановить unit, сохранить резервную копию SQLite и каталогов прогонов, обновить checkout до выбранного SHA, повторить `uv sync` и запустить unit. Для отката использовать предыдущий SHA и совместимую резервную копию состояния. Проверять восстановление активного прогона после рестарта отдельно от первого запуска.
