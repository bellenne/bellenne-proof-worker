# Контракт данных Proof Core ↔ Proof Worker

Версия протокола: `v1`.

## Общие правила

- Базовый URL задаётся без `/api/v1`, например `https://host/proof`.
- Все методы используют `POST`.
- Все запросы Worker содержат `Authorization: Bearer <worker-token>`.
- JSON передаётся с `Content-Type: application/json`, кроме загрузки результата.
- Неизвестные поля в ответах Core допускаются и игнорируются Worker.
- Идентификатор Job: 1–128 символов, первый символ буквенно-цифровой, остальные —
  буквы, цифры, `_` или `-`.

## Объект Job

Core возвращает Job в ответах `claim`, `start`, `complete` и `fail`.

```json
{
  "id": "bf6f62de-4a6c-4d8e-b9c3-3930e2a6cb66",
  "processing_status": "assigned",
  "delivery_status": "pending",
  "attempt": 1,
  "input": {
    "source_path": "\\\\ip\\дизайн отдел\\Макеты (опт)\\Сентябрь 2026\\33860843",
    "schema": "bellenne-proof/v2",
    "items": [{
      "id": "proof-1",
      "position": 0,
      "layout_number": "3",
      "proof_variant": "fragment_90x30",
      "brightness_direction": null,
      "brightness_percent": null,
      "fragments": [
        {"id": "proof-1:fragment:1", "position": 0, "proof_variant": "fragment_30x30", "brightness_direction": null, "brightness_percent": null},
        {"id": "proof-1:fragment:2", "position": 1, "proof_variant": "fragment_30x30_color", "brightness_direction": "add", "brightness_percent": 5},
        {"id": "proof-1:fragment:3", "position": 2, "proof_variant": "fragment_30x30_color", "brightness_direction": "subtract", "brightness_percent": 5}
      ]
    }],
    "order_number": "12345",
    "public_id": "CRM-12345",
    "metadata": {}
  },
  "preset": {
    "id": "preset-rgb",
    "name": "Цветопроба 600x300",
    "version": 1,
    "parameters": {}
  },
  "crm_order_id": "12345",
  "source": "amocrm",
  "progress": null,
  "current_stage": ""
}
```

Обязательные поля:

| Поле | Тип | Ограничения |
| --- | --- | --- |
| `id` | string | Непустой валидный идентификатор Job |
| `processing_status` | string | `received`, `queued`, `assigned`, `running`, `completed`, `failed`, `cancelled`, `retrying` |
| `delivery_status` | string | `pending`, `delivering`, `delivered`, `failed`, `retrying` |
| `attempt` | integer | `>= 1` |
| `input.source_path` | string | Непустой относительный путь либо полный UNC-путь из сделки, максимум 2048 символов |
| `input.schema` | string | Для заявки виджета ровно `bellenne-proof/v2` |
| `input.items` | array[object] | От 1 до 100 позиций в порядке `position`; номер макета хранится строкой цифр от `1` до `999999` |
| `preset` | object | Снимок preset для этой попытки |
| `preset.id` | string | Идентификатор preset |
| `preset.name` | string | Название preset |
| `preset.version` | integer | `>= 1` |
| `preset.parameters` | object | Параметры по схеме текущей версии Worker |

Необязательные поля `input`:

| Поле | Тип | Default / назначение |
| --- | --- | --- |
| `order_number` | string | Если отсутствует, используется `crm_order_id` верхнего уровня |
| `public_id` | string | `""` |
| `metadata` | object | `{}` |
| `brightness_direction` | string или null | Для `fragment_30x30_color` обязательно: `add` или `subtract`; управляет насыщенностью согласно производственному сленгу |
| `brightness_percent` | number или null | Процент изменения насыщенности: больше `0`, не больше `100` |
| `layout_number` | integer | Устаревший одиночный вариант; используется, если `layout_numbers` отсутствует |
| `layout_numbers` | array[integer] | Совместимый старый формат с общим вариантом обработки |
| `proof_variant` | string | Общий вариант для старого формата |

Для amoCRM `source_path` передаётся как полный UNC-путь из выбранного поля
сделки. Core-конфигурация Worker содержит соответствие UNC-префикса локальному
read-only mount. После безопасной замены префикса путь не содержит номер
ревизии, каталог `Исходник` или имя файла. Артикул в контракт не входит.

Worker обрабатывает все `layout_numbers` в одной новой ревизии заказа и загружает
в Core один ZIP с подписанными JPEG из каталога `Превью`. Core проверяет архив,
загружает эти же байты в Яндекс.Диск и добавляет публичную ссылку в сделку.
Повтор CRM-доставки не запускает обработку изображения на Worker заново.

В новом формате Worker обрабатывает все `input.items` по порядку. Для
`fragment_90x30` обязательны ровно три вложенных фрагмента. Каждый использует
один и тот же выбранный участок 30×30; `fragment_30x30` оставляет его исходным,
а `fragment_30x30_color` применяет собственные `brightness_direction` и
`brightness_percent`. Три панели объединяются слева направо в JPEG 90×30.

`fragment_30x30` создаёт один квадратный фрагмент 30×30 см с DPI preset.
`thumbnail` уменьшает или увеличивает весь макет с сохранением пропорций: большая
сторона результата равна 30 см при 150 DPI. Обзорная вставка поверх такого
результата не добавляется.

Полный эталон `preset.parameters` зафиксирован в
[`examples/preset.json`](../examples/preset.json).

## Heartbeat

### Запрос

`POST /api/v1/workers/heartbeat`

```json
{
  "hostname": "proof-worker-01",
  "version": "0.1.0",
  "availability": "busy",
  "capabilities": ["rgb"],
  "current_job_id": "bf6f62de-4a6c-4d8e-b9c3-3930e2a6cb66",
  "last_error_code": null,
  "last_error_message": null
}
```

| Поле | Тип | Ограничения |
| --- | --- | --- |
| `hostname` | string | 1–255 символов |
| `version` | string | 1–80 символов |
| `availability` | string | `available`, `busy`, `error` |
| `capabilities` | array[string] | Не более 100 элементов |
| `current_job_id` | string или null | Текущий назначенный Job |
| `last_error_code` | string или null | До 120 символов |
| `last_error_message` | string или null | До 2000 символов |

### Ответ `200`

```json
{
  "id": "worker-1",
  "name": "Proof Worker 01",
  "hostname": "proof-worker-01",
  "version": "0.1.0",
  "online": true,
  "availability": "busy",
  "current_job_id": "bf6f62de-4a6c-4d8e-b9c3-3930e2a6cb66",
  "heartbeat_timeout_seconds": 120,
  "capabilities": ["rgb"],
  "configuration_version": 3,
  "configuration": {
    "heartbeat_interval": 30,
    "poll_interval": 5,
    "retry_initial_seconds": 1,
    "retry_max_seconds": 30,
    "storage_retry_limit": 5,
    "file_not_found_retry_limit": 0,
    "health_interval": 5,
    "health_max_age": 90,
    "wake_timeout_seconds": 5,
    "path_mappings": [
      {
        "source_prefix": "\\\\Wallpaper\\10.08 Полноразмерки",
        "local_root": "/sources/main"
      }
    ]
  }
}
```

`availability` принимает `available`, `busy` или `error`;
`heartbeat_timeout_seconds` — целое число `>= 1`. Worker применяет только более
новую `configuration_version`; каждый `local_root` обязан входить в bootstrap
allowlist `WORKER_SOURCE_ROOTS`.

Если `input.source_path` содержит полный UNC-путь, `configuration.path_mappings`
должен содержать соответствующий `source_prefix`. Worker удаляет этот префикс и
использует оставшуюся часть как относительный путь внутри `local_root`.

## Push-сигнал очереди

Core может ускорить получение нового Job запросом `POST /wake` на callback URL
конкретного Worker. Тело сигнала:

```json
{"event":"queue.changed","job_id":"bf6f62de-4a6c-4d8e-b9c3-3930e2a6cb66"}
```

Заголовки `X-Proof-Timestamp` и `X-Proof-Signature` обязательны. Подпись —
HMAC-SHA256 от `<timestamp>.<raw body>` с ключом SHA-256 токена Worker. Сигнал не
назначает Job и не содержит его данных: после него Worker выполняет обычный
аутентифицированный `claim`. При недоступности callback остаётся polling.

## Получение задания

### Запрос

`POST /api/v1/jobs/claim`

Тело отсутствует.

### Ответы

- `200` + объект Job со статусом `assigned` или `running`;
- `204 No Content`, если доступного или уже назначенного Job нет.

## Начало обработки

### Запрос

`POST /api/v1/jobs/{job_id}/start`

Тело отсутствует.

### Ответ `200`

Объект Job с тем же `id` и `processing_status: "running"`.

## Прогресс

### Запрос

`POST /api/v1/jobs/{job_id}/progress`

```json
{
  "progress": 65,
  "current_stage": "RENDERING"
}
```

| Поле | Тип | Ограничения |
| --- | --- | --- |
| `progress` | integer | От `0` до `100` |
| `current_stage` | string | До 160 символов |

### Ответ `200`

```json
{
  "job_id": "bf6f62de-4a6c-4d8e-b9c3-3930e2a6cb66",
  "progress": 65,
  "current_stage": "RENDERING"
}
```

Ответ должен дословно подтверждать `job_id`, `progress` и `current_stage`.

## Событие Job

### Запрос

`POST /api/v1/jobs/{job_id}/events`

```json
{
  "event_type": "render.started",
  "level": "info",
  "message": "Rendering",
  "error_code": null,
  "details": {
    "width": 1701
  }
}
```

| Поле | Тип | Ограничения |
| --- | --- | --- |
| `event_type` | string | 1–120 символов |
| `level` | string | `debug`, `info`, `warning`, `error`, `critical` |
| `message` | string | 1–4000 символов |
| `error_code` | string или null | До 120 символов |
| `details` | object | JSON object |

### Ответ `200`

```json
{
  "status": "recorded"
}
```

## Загрузка результата

### Запрос

`POST /api/v1/jobs/{job_id}/result`

Заголовок:

```http
Idempotency-Key: <64 lowercase hex characters>
```

Формат тела: `multipart/form-data`.

| Part | Тип | Требование |
| --- | --- | --- |
| `file` | binary | ZIP с JPEG из папки `Превью`, `Content-Type: application/zip` |
| `metadata_json` | string | JSON object с обязательными `sha256` и `attempt` |

Минимальный `metadata_json`:

```json
{
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "attempt": 1
}
```

`sha256` — SHA-256 фактически переданных байтов файла, 64 lowercase hex-символа.
`attempt` должен совпадать с попыткой Job.

Worker также передаёт `result_kind: "preview_archive"`, `published_revision` и
`published_files`. Каждый элемент `published_files` содержит номер макета, имя,
пути опубликованных исходника и превью, размер и SHA-256 превью. Каждый JPEG из
`Превью` содержит исходную ЦП целиком и добавленную снизу белую полосу с чёрной
подписью имени файла без расширения.

Стандартный ключ идемпотентности:

```text
SHA256("proof-worker-v1:{job_id}:{attempt}:{sha256}")
```

Повтор с тем же ключом и теми же байтами должен возвращать ранее созданный
Result и не создавать дубликат.

### Ответы

- `201` — Result создан;
- `200` — идемпотентный повтор существующего Result.

```json
{
  "result_id": "result-1",
  "duplicate": false,
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

`result_id` непустой, `duplicate` имеет тип boolean, `sha256` должен совпадать с
контрольной суммой отправленного файла.

## Успешное завершение

### Запрос

`POST /api/v1/jobs/{job_id}/complete`

Тело отсутствует.

### Ответ `200`

Объект Job с тем же `id` и `processing_status: "completed"`.

## Завершение с ошибкой

### Запрос

`POST /api/v1/jobs/{job_id}/fail`

```json
{
  "error_code": "FILE_NOT_FOUND",
  "message": "Source file was not found",
  "details": {}
}
```

| Поле | Тип | Ограничения |
| --- | --- | --- |
| `error_code` | string | 1–120 символов |
| `message` | string | 1–4000 символов |
| `details` | object | JSON object |

### Ответ `200`

Объект Job с тем же `id` и `processing_status: "failed"`.

## Ошибки HTTP

Core возвращает JSON-ошибку в собственном стандартном формате и корректный
HTTP status. Для протокола значимы следующие статусы:

| HTTP status | Значение |
| --- | --- |
| `400`, `422` | Некорректный запрос или несовместимый контракт |
| `401`, `403` | Ошибка токена или прав Worker |
| `404` | Job или маршрут не найден |
| `409` | Job не принадлежит Worker либо находится в несовместимом состоянии |
| `429` | Временное ограничение запросов |
| `500`–`599` | Временная ошибка Core |

Redirect-ответы не являются частью контракта. Таймаут или разрыв соединения при
мутации не подтверждает, что Core отклонил запрос; повтор результата выполняется
с тем же `Idempotency-Key` и теми же байтами.
