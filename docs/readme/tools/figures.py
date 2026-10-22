"""Фигуры README: какие кадры, обрезки и пометки. Запуск: figures.py <каталог docs/readme> <каталог кадров> [имена фигур]"""
import sys
from pathlib import Path

from annot import Fig

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "out"); SHOTS = Path(sys.argv[2] if len(sys.argv) > 2 else "shots")


def header():
    f = Fig(SHOTS, "revision", crop=(0, 0, 1100, 56), extend=(0, 0, 0, 180))
    f.callout(1, f.find("Fleet Console", "a"), "главная: ревизия fleet", (16, 150), size=13)
    f.callout(2, f.find("internal", ".q-chip"), "kube context · namespace\nклик — статус API и права", (150, 80), size=13)
    f.callout(3, f.find("бастион", ".q-chip"), "ssh-туннель к кластеру\nзелёный = работает", (330, 150), size=13)
    f.callout(4, f.find("Кластер", "a"), "дашборд кластера", (520, 80), size=13)
    f.callout(5, f.find("Модели", "a"), "сессии моделей", (610, 150), size=13)
    f.callout(6, f.find("Прогоны", "a"), "бенч-прогоны", (760, 80), size=13)
    f.callout(7, f.find("Актуализировать", "button"), "git fetch зеркала fleet", (830, 150), size=13)
    f.callout(8, f.find("Настройки", "a"), "настройки", (1090, 80), anchor="rt", size=13)
    f.save(OUT / "01-header.jpg")


def revision():
    f = Fig(SHOTS, "revision", crop=(0, 0, 1100, 720))
    f.callout(1, f.find("Ветка", ".q-field"), "ветка", (200, 92), size=13)
    f.callout(2, f.find("Коммит", ".q-field"), "коммит · или SHA/тег в поле справа", (430, 92), size=13)
    f.callout(3, f.find("Подготовить ревизию", "button"), "worktree + uv sync + профиль ревизии", (1046, 210), anchor="rt", size=13)
    f.callout(4, f.find("Готово:", ".text-lg"), "ревизия готова: версия Harbor, образ раннера, шаги fleet", (320, 262), size=13)
    f.callout(5, f.find("Каталог конфигов", ".text-lg"), "все конфиги этой ревизии с описаниями из файлов", (320, 393), size=13)
    f.save(OUT / "02-revision.jpg")


def catalog():
    f = Fig(SHOTS, "revision-catalog", crop=(0, 480, 1100, 1090))
    f.callout(1, (85, 664, 620, 712), "описание — из комментария в шапке файла;\nраскрыть — полный текст файла", (660, 650), size=13)
    f.save(OUT / "03-catalog.jpg")


def kube_dialog():
    f = Fig(SHOTS, "header-kube", crop=(110, 600, 1000, 1250))
    f.callout(1, f.find("Кластер", ".text-lg"), "клик по зелёному чипу\nв шапке", (125, 615), size=13)
    f.callout(2, (322, 920, 790, 1162), "права проверены: галочка —\nправо есть, крестик — нет", (125, 1000), size=13)
    f.save(OUT / "04-kube-dialog.jpg")


def cluster():
    f = Fig(SHOTS, "cluster")
    f.callout(1, (791, 96, 1068, 136), "показать и завершённые · опросить сейчас", (770, 100), anchor="rt", size=13)
    f.callout(2, (48, 164, 330, 200), "прогоны всех в Argo · клик по строке —\nусыновить чужой или остановить свой", (420, 160), size=13)
    f.callout(3, (48, 515, 330, 551), "поднятые модели и занятые GPU", (420, 515), size=13)
    f.callout(4, (48, 956, 330, 992), "сколько реплик каждой модели в маршруте", (420, 956), size=13)
    f.callout(5, (48, 1189, 330, 1225), "очередь задач и лимит агентов · клик — лог сабмиттера", (420, 1189), size=13)
    f.callout(6, (48, 1426, 330, 1462), "узлы пула type=bench", (420, 1426), size=13)
    f.save(OUT / "05-cluster.jpg")


def models():
    f = Fig(SHOTS, "models")
    f.callout(1, f.find("Поднять модель", "button"), "поднять модель без бенча", (1030, 146), anchor="rb", size=13)
    f.callout(2, f.find("Сессия", ".q-table"), "ваши сессии · клик по строке — карточка:\nстадии, команды, логи, продление, гашение", (300, 150), size=13)
    f.callout(3, f.find("Пробросить порт", "button"), "адрес модели на localhost\nдля своих скриптов", (740, 378), size=13)
    f.callout(4, f.find("Песочница", ".text-lg"), "отправить модели запрос прямо отсюда", (300, 520), size=13)
    f.save(OUT / "06-models.jpg")


def model_new():
    f = Fig(SHOTS, "models-new")
    _, y0, _, y1 = f.find("", ".q-dialog .q-card")
    f = Fig(SHOTS, "models-new", crop=(0, y0 - 40, 1100, y1 + 40))
    f.callout(1, f.find("Рецепт mlsc/model", ".q-field"), "рецепт модели\nиз конфигов fleet", (20, y0 + 70), size=13)
    f.callout(2, (f.find("Реплики", ".q-field")[0], f.find("Реплики", ".q-field")[1], f.find("Время жизни", ".q-field")[2], f.find("Время жизни", ".q-field")[3]),
              "сколько инстансов\nи на сколько минут", (20, f.find("Реплики", ".q-field")[1]), size=13)
    f.callout(3, f.find("Запустить подъём", "button"), "перед стартом —\nдиалог подтверждения\nс числом GPU", (858, f.find("Запустить подъём", "button")[1] - 30), size=13)
    f.save(OUT / "07-model-new.jpg")


def gateway():
    f = Fig(SHOTS, "models-gateway", crop=(0, 372, 1100, 912))
    b = f.find("Закрыть проброс", "button")
    f.callout(1, b, "та же кнопка закрывает проброс;\nв шапке появляется чип «шлюз»", (800, 395), size=13)
    f.callout(2, (71, 478, 590, 502), "адрес для OpenAI-совместимых клиентов", (620, 476), size=13)
    f.callout(3, (85, 572, 160, 592), "готовые примеры: curl и OpenAI SDK", (300, 568), size=13)
    f.save(OUT / "08-gateway.jpg")


def runs():
    f = Fig(SHOTS, "runs", crop=(0, 0, 1100, 330))
    f.callout(1, f.find("Запустить бенч", "button"), "форма нового прогона", (900, 98), anchor="rt", size=13)
    f.callout(2, f.find("Прогон", ".q-table"), "клик по строке — карточка прогона", (360, 250), size=13)
    f.save(OUT / "09-runs.jpg")


def run_form():
    f = Fig(SHOTS, "run-form", extend=(0, 0, 340, 0))
    X = 1010
    f.callout(1, f.find("Пресет", ".q-field"), "пресет заполняет\nвсе поля ниже", (X, 170), size=13)
    f.callout(2, (118, 272, 982, 472), "семь полей: модель, агент,\nдатасет, реплики, агентов\nна реплику, лимит задач", (X, 300), size=13)
    f.callout(3, (118, 528, 982, 564), "overrides и полный\nконфиг с правкой", (X, 520), size=13)
    f.callout(4, (118, 600, 982, 996), "превью: что и где\nзапустится — до того,\nкак заняты GPU", (X, 760), size=13)
    f.callout(5, (118, 1008, 982, 1302), "проверки: красные блокируют\nзапуск, жёлтые можно принять", (X, 1130), size=13)
    f.callout(6, f.find("Принимаю предупреждения", ".q-checkbox"), "принять жёлтые\nпредупреждения", (X, 1322), size=13)
    f.callout(7, (118, 1380, 470, 1416), "сначала «Превью и preflight»,\nпотом «Запустить»", (X, 1388), size=13)
    f.save(OUT / "10-run-form.jpg")


def run_config():
    f = Fig(SHOTS, "run-config-resolved", extend=(0, 0, 340, 0))
    X = 1075
    f.callout(1, (70, 230, 1030, 632), "exp.yaml — эксперимент fleet;\nможно править прямо здесь", (X, 300), size=13)
    f.callout(2, f.find("Сохранить и пересобрать", "button"), "проверка YAML и сборка\nконфига после правки", (X, 648), size=13)
    f.callout(3, (70, 748, 1030, 1112), "файлы конфигов ревизии\nв порядке применения;\nраскрыть — текст с комментариями\nи «Править в этом прогоне»", (X, 860), size=13)
    f.callout(4, (70, 1208, 1030, 1562), "итоговый конфиг деревом:\nклик по ключу — откуда\nпришло значение", (X, 1330), size=13)
    f.save(OUT / "11-run-config.jpg")


def run_card():
    f = Fig(SHOTS, "run-card-open", extend=(0, 0, 340, 0))
    X = 1010
    f.callout(1, (112, 136, 800, 162), "статус, ревизия, версия\nмодели в MLSC, время", (X, 120), size=13)
    f.callout(2, (136, 228, 966, 652), "то же превью, но с\nфактическими значениями", (X, 400), size=13)
    f.callout(3, (112, 682, 900, 772), "ссылки: Argo UI, папка в S3,\nкаталог прогона на диске", (X, 700), size=13)
    f.callout(4, (112, 834, 966, 1036), "задачи: статус, попытки\nв S3, логи контейнеров", (X, 920), size=13)
    f.callout(5, (112, 1050, 966, 1238), "стадии fleet: точная\nкоманда и хвост лога", (X, 1130), size=13)
    f.callout(6, (136, 1550, 966, 1588), "журнал событий прогона", (X, 1550), size=13)
    f.save(OUT / "12-run-card.jpg")


def settings():
    f = Fig(SHOTS, "settings", crop=(0, 0, 1100, 830), extend=(0, 0, 300, 0))
    X = 950
    f.callout(1, f.find("Remote fleet", ".q-field"), "откуда брать fleet", (X, 140), size=13)
    f.callout(2, f.find("ssh host бастиона", ".q-field"), "имя хоста из ~/.ssh/config", (X, 332), size=13)
    f.callout(3, f.find("Корень датасетов", ".q-field"), "где лежат задачи\nдатасетов на этой машине", (X, 516), size=13)
    f.callout(4, f.find("Профиль ключей S3", ".q-field"), "профиль ~/.aws/credentials\nдля чтения S3", (X, 590), size=13)
    f.callout(5, (182, 644, 530, 700), "раундов requeue · через сколько\nминут погасить зависшую\nмодель · период опроса", (X, 646), size=13)
    f.callout(6, f.find("Сохранить", "button"), "часть настроек применится\nпосле перезапуска сервиса", (X, 760), size=13)
    f.save(OUT / "13-settings.jpg")


FIGS = {"header": header, "revision": revision, "catalog": catalog, "kube_dialog": kube_dialog, "cluster": cluster, "models": models,
        "model_new": model_new, "gateway": gateway, "runs": runs, "run_form": run_form, "run_config": run_config, "run_card": run_card,
        "settings": settings}
if __name__ == "__main__":
    only = sys.argv[3:] or list(FIGS)
    for name in only:
        try:
            FIGS[name]()
        except Exception as exc:  # noqa: BLE001 — остальные фигуры всё равно рисуем
            print("FAIL", name, repr(exc)[:300])
