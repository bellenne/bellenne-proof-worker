# Контракт Proof Core v1

Клиент сверён с существующим сервером `D:/Works/Python/BellenneOne/apps/proof`:
`app/main.py` (`job_payload`, `worker_payload`, Worker routes), `app/schemas.py`
и `app/services.py` (`claim_next_job`, `require_owned_job`, `store_result`).
Серверные файлы не менялись. Тесты `tests/test_core_client.py` проверяют HTTP
контракт через `httpx.MockTransport`; они не означают проверку живого развёртывания.

`PROOF_CORE_URL` указывает на корень модуля, например `https://host/proof/` через
Bellenne gateway либо `http://proof:8000/` при прямом подключении. Клиент сохраняет
этот префикс и добавляет пути ниже. Все запросы используют
`Authorization: Bearer <PROOF_WORKER_TOKEN>`. Имя Worker и его идентификатор
принадлежат регистрации токена в Core; heartbeat не переименовывает Worker.

| POST, относительно корня модуля | Запрос | Подтверждение |
| --- | --- | --- |
| `api/v1/workers/heartbeat` | `hostname`, `version`, `availability`, `capabilities`, `current_job_id`, последние ошибки | Worker JSON с версией runtime-конфигурации |
| `api/v1/jobs/claim` | Без тела | Job JSON либо `204` |
| `api/v1/jobs/{job_id}/start` | Без тела | Job со статусом `running` |
| `api/v1/jobs/{job_id}/progress` | `progress`, `current_stage` | `job_id`, `progress`, `current_stage` |
| `api/v1/jobs/{job_id}/events` | `event_type`, `level`, `message`, `error_code`, `details` | `{"status":"recorded"}` |
| `api/v1/jobs/{job_id}/result` | Multipart `file`, `metadata_json`; заголовок `Idempotency-Key` | `result_id`, `duplicate`, `sha256`; `201` либо `200` для повтора |
| `api/v1/jobs/{job_id}/complete` | Без тела | Job со статусом `completed` |
| `api/v1/jobs/{job_id}/fail` | `error_code`, `message`, `details` | Job со статусом `failed` |

Worker `IDLE/BUSY/ERROR` передаётся как Core `available/busy/error`.
Heartbeat обязан сообщать именно текущий `current_job_id` Core, иначе сервер
возвращает `409`. Поэтому синхронизация назначения предшествует heartbeat.

Job содержит `id`, `attempt`, `processing_status`, `delivery_status`, `input`
и снимок `preset: {id, name, version, parameters}`. Для поиска исполнитель берёт
из `input` обязательные `source_path` и `layout_number`. Для amoCRM Job
`source_path` может быть полным UNC-путём; Worker заменяет настроенный Core
UNC-префикс на разрешённый локальный mount. Без совпавшего mapping путь остаётся
относительным к roots preset для обратной совместимости. `layout_number` — число из
связки `Макет N`. Артикул для поиска не передаётся. Номер заказа берётся из
`input.order_number` с резервным `crm_order_id`; `public_id` и `metadata`
необязательны.

Внутри папки заказа Worker рассматривает непосредственные числовые каталоги как
ревизии и рекурсивно ищет макеты во всех их подпапках. Core не добавляет к
`source_path` номер ревизии или имя каталога с исходниками: эти части структуры
обнаруживает Worker.
Готовый JPEG записывается в `Исходник` новой непосредственной числовой ревизии
папки заказа, а подписанная копия — в `Превью` этой же ревизии. В Core Worker
отправляет подписанный JPEG из `Превью`. В metadata результата Worker передаёт
`published_revision`, `published_filename`, `published_source_path`,
`published_preview_path`, совместимый alias `published_path` и SHA-256 превью.
`preset.parameters` должен соответствовать модели Worker `Preset`; пример лежит
в `examples/preset.json`. Неизвестные поля параметров отклоняются явно.
Формат Core JSON отделён от внутренних моделей и не передаётся в ошибки целиком.

## Восстановление и ограничения существующего сервера

Core v1 не предоставляет Worker GET для проверки сохранённого Job или Result.
`claim` возвращает текущее назначение в `assigned/running`, если оно существует;
иначе может назначить следующий Job. Это запрос с побочным эффектом, а не
произвольный поиск. После перезапуска и неоднозначного сетевого сбоя Worker
сначала вызывает `claim` и сохраняет полученные идентификатор и попытку.

Повторно использовать сохранённый артефакт можно только при совпадении
`job_id + attempt` с актуальным ответом Core. Другие локальные записи остаются
`DETACHED` для разбора оператором; по ним нельзя вслепую отправлять `start`,
`complete`, `fail` или результат. Это особенно существенно после потери ответа
`complete/fail`: сервер мог уже освободить Worker, а следующий `claim` — выдать
новое задание. Локальная запись сама по себе не доказывает старое назначение.

В v1 нет lease/fencing token и проверки ожидаемой попытки при мутациях. Поле
`attempt` в `metadata_json` информационное: сервер сохраняет текущую попытку
Job и не сравнивает её с metadata. Поэтому абсолютную защиту от гонки между
`claim` и последующей мутацией нельзя обеспечить одним клиентом. Нужен будущий
контракт Core с lease/expected-attempt, read-only reconciliation и выдачей
результатов по попытке. До этого один токен используют только на одном Worker;
ручной retry/переназначение активного Job требует остановки его исполнителя.

Core дедуплицирует Result по `(worker_id, Idempotency-Key)` и допускает один
Result на `(job_id, attempt)`. Worker сохраняет детерминированный ключ
`SHA256("proof-worker-v1:{job_id}:{attempt}:{sha256}")` и повторяет загрузку
с тем же ключом и файлом после синхронизации назначения. Перед отправкой клиент
проверяет SHA-256 файла, после ответа — SHA-256 подтверждения. Успешное
завершение обработки не зависит от `delivery_status`: доставку в CRM выполняет
Core.

HTTP-клиент не делает скрытых повторов и не следует перенаправлениям.
Timeout, транспортный сбой, `408/425/429/5xx` и повреждённое подтверждение
считаются неоднозначными: мутация могла сохраниться. Ограниченные повторы и
журнал намерений находятся в сервисе. `401/403/422` означают ошибку настройки,
`404/409` — необходимость сверить состояние. Тексты ответов, HTTP headers и
исходные исключения не включаются в WorkerError; перед отправкой сообщений и
metadata редактируются токен Worker и значения чувствительных ключей.
