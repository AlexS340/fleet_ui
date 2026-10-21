Свои группы Hydra поверх групп ревизии fleet: `harbor/agent/*.yaml`, `mlsc/model/*.yaml`, `datasets/*.yaml`
(реестр local→S3), при желании `harbor/dataset/*.yaml`. Подключаются к каждой стадии через второй элемент
`hydra.searchpath`; имена не должны совпадать с именами файлов ревизии — это проверяет preflight.
`console_model.yaml` — конфиг сессии модели (раздел «Модель без бенча»).
