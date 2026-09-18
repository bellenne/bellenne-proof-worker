# Bellenne Proof Worker

Отдельный исполнитель цветопробы для BellenneProof Core. Один долгоживущий
процесс получает одно назначенное задание, находит исходный макет, выбирает
фрагмент, создаёт RGB JPEG и передаёт результат Core. Очередь, бизнесовые статусы,
presets и доставка в CRM принадлежат Core. У Worker нет UI, CRM credentials и
доступа к БД BellenneOne. Входящий HTTP endpoint `/wake` принимает только
подписанный Core сигнал о появлении работы; само назначение Worker по-прежнему
получает атомарным `claim` через Core API.

## Результат обработки

По умолчанию весь JPEG — фрагмент оригинала **600 × 300 мм, 72 DPI**, то есть
`round(mm / 25.4 * dpi)` = **1701 × 850 px**. Масштаб главного фрагмента 1:1:
один source pixel соответствует одному пикселю результата; исходный DPI
сохраняется только в диагностике. Маленький source завершается `SOURCE_TOO_SMALL`.

Поверх фрагмента располагается миниатюра **всего** макета с максимальной стороной
150 мм, слева на 50 мм, по вертикали в центре. Чёрная рамка и прямоугольник
выбранного фрагмента имеют толщину 2 px. Полей, текста, логотипов и цветовых шкал нет.

CV работает только с уменьшенным RGB preview (до 2000 px по длинной стороне).
Шесть независимых оценок учитывают контент, детали, цвета, значимые области,
пересечение значимых областей с границей и небольшой приоритет центра. Кандидаты
имеют фиксированный размер относительно оригинала. Возвращаются лучший вариант,
до трёх различных вариантов после NMS, компоненты оценки и confidence.
Если допустимых вариантов меньше трёх, алгоритм не выдумывает альтернативы.
Однотонный макет может дать `ANALYSIS_NO_VALID_CROP`.

## Форматы и RGB / ICC

- JPEG, PNG, одностраничный TIFF/BigTIFF; содержимое RGB или RGBA, unsigned 8/16-bit.
- CMYK, Lab, grayscale, indexed/palette, неоднозначные каналы и неподдерживаемые
  типы сэмплов отклоняются. PSD, PSB и PDF не поддерживаются.
- ICC RGB сохраняется в JPEG без преобразования между профилями. Профиль sRGB
  автоматически не назначается. Без ICC обработка продолжается с
  `ICC_PROFILE_MISSING`; повреждённый ICC даёт `INVALID_IMAGE`, а ICC другого
  цветового пространства — `UNSUPPORTED_COLOR_SPACE`.
- RGBA компонуется с `alpha_background` из preset. JPEG 8-bit: для unsigned
  16-bit RGB используется явное линейное приведение диапазона сэмплов к 8-bit,
  отражённое в source metadata (`source_sample_format`); ICC не меняется.
- EXIF orientation не поворачивает пиксели автоматически. Preview, crop и
  thumbnail используют координаты исходной пиксельной матрицы; результат не
  наследует EXIF rotation и устаревшие thumbnails.
- Финальные пиксели извлекаются libvips из оригинала. OpenCV preview никогда
  не используется для production-рендера. Полный source не переносится в NumPy.

## Требования

Production: Docker Engine / Docker Desktop с Linux containers, доступ к Core,
read/write mount папок заказов, постоянные volumes `/data` и `/output`.
Для разработки: Python 3.12+, libvips, зависимости из `requirements-dev.txt`.

## Запуск через Compose

1. В Core зарегистрируйте отдельный Worker и получите его токен. Задайте timeout
   heartbeat заметно больше 30 секунд, например 120 секунд.
2. Скопируйте `.env.example` в `.env`. Заполните `PROOF_CORE_URL`,
   `PROOF_WORKER_TOKEN`, `PROOF_WORKER_NAME`, `SOURCE_MAIN_HOST_PATH`.
3. Создайте preset в Core, используя [examples/preset.json](examples/preset.json).
   Core выдаёт непрозрачный JSON: поля `contract: worker-v1` из серверных тестовых
   fixtures не являются production preset этого исполнителя.
4. Запустите:

```sh
docker compose up -d --build
docker compose logs -f proof-worker
docker compose ps
```

`PROOF_CORE_URL` — корень модуля, например `https://bellenne.example/proof`
через gateway или `http://proof:8000` напрямую. Не добавляйте `/api/v1`.
Для Core на Windows-хосте Docker Desktop используйте `host.docker.internal`
и фактический порт, например `http://host.docker.internal:17863/proof`.
`localhost` внутри контейнера относится к самому контейнеру.

Если Worker и BellenneOne запущены на одном Docker-хосте, подключите Worker к
сети Core без публикации дополнительных портов:

```sh
docker compose \
  -f docker-compose.yml \
  -f docker-compose.bellenne.yml \
  up -d --build
```

Для стандартного имени проекта BellenneOne сеть называется
`bellenneone_default`. Если она переопределена, задайте `BELLENNE_NETWORK` в
`.env`. В этом режиме используется `PROOF_CORE_URL=http://proof:8000`; адрес
`proof` — внутреннее DNS-имя сервиса Core. Сеть должна уже существовать, то есть
BellenneOne запускается раньше Worker.

Core Job amoCRM содержит полный UNC-путь папки заказа и снимок заявки виджета:

```json
{
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
    }]
  }
}
```

`proof_variant` принимает `fragment_90x30`, `fragment_60x30`, `two_fragments_30x30`,
`fragment_30x30_color`, `fragment_30x30` или `thumbnail`. Для варианта с
цветокоррекцией дополнительно передаются
`brightness_direction` (`add`/`subtract`) и `brightness_percent` (`> 0`, `<= 100`).
Несмотря на имя полей amoCRM, они изменяют насыщенность, а не светлоту изображения.
`fragment_90x30` использует один автоматически выбранный участок 30×30: первая
панель остаётся без изменений, две следующие получают независимые настройки
насыщенности из `fragments`. Старые `layout_numbers`, `layout_number` и общий
`proof_variant` продолжают поддерживаться.
`thumbnail` сохраняет весь макет с пропорциями, 30 см по большей стороне и 150 DPI.

На странице Core **Proof → Workers** настройте соответствие UNC-префикса
`\\ip\дизайн отдел` каталогу `/sources/main`. Worker заменяет только этот
префикс, проверяет mount по bootstrap allowlist и получает относительную часть
`Макеты (опт)/Сентябрь 2026/33860843`. Пути вне разрешённых mounts не
принимаются. Необязательны `input.order_number`, `input.public_id`,
`input.metadata`. Номер заказа также может читаться из `crm_order_id`.
Снимок production-параметров приходит в `preset.parameters`. Неизвестные поля
preset отклоняются, чтобы не игнорировать настройку незаметно.

Сборка/запуск без Compose:

```sh
docker build -t bellenne-proof-worker:0.1.0 .
docker run -d --name proof-worker --restart unless-stopped --init \
  --env-file .env \
  --mount type=volume,source=proof-data,target=/data \
  --mount type=volume,source=proof-output,target=/output \
  --mount type=bind,source=/mnt/artworks,target=/sources/main \
  bellenne-proof-worker:0.1.0
```

Контейнер работает с UID/GID `10001:10001`. Named volumes получают необходимые
права из образа. Для bind-mount `/data`, `/output` и `/sources/main` задайте этому
пользователю доступ записи. Один токен и один recovery volume
используются только одним экземпляром Worker. Process lock препятствует второму
процессу в том же `/data`. Масштабирование — отдельные токены и volumes.

## Windows, SMB и несколько источников

Windows-пути указываются **только на стороне Docker**:

```dotenv
SOURCE_MAIN_HOST_PATH=D:/Production/Artworks
```

Для NAS/SMB сначала подключите share на хосте с правами, доступными Docker,
затем смонтируйте его в `/sources/main`. Credentials SMB настраиваются на хосте,
в код и preset не попадают. Для Linux это может быть заранее подключённый
`/mnt/production`. Не создавайте пустую локальную папку взамен недоступного share.
Compose использует `create_host_path: false` и не создаёт источник автоматически.

Чтобы добавить ещё одно хранилище, добавьте доступный для записи bind mount
`/sources/archive` и укажите:

```yaml
environment:
  WORKER_SOURCE_ROOTS: '["/sources/main", "/sources/archive"]'
```

В `preset.search.roots` разрешены только эти mounts или их подкаталоги. К ним
Worker добавляет относительный `input.source_path`. Job metadata не может
подменить source произвольным путём. Симлинки за пределы root и обход через
`../` не используются. Найденный исходный файл не изменяется.

Для NAS, который при отключении оставляет доступную пустую mountpoint-папку,
положите на share файл-маркер, например `.proof-storage-online`, и задайте
`preset.search.storage_marker` этим именем. Без маркера ОС не всегда позволяет
отличить отключённый share от действительно пустой папки.

В папке заказа Worker рассматривает непосредственные числовые каталоги `1`, `2`,
`3` и так далее как последовательные ревизии работы. Внутри каждой ревизии он
рекурсивно проверяет все вложенные папки. Название папки с исходниками не является
частью контракта: допустимы `Исходник`, `Исходники`, `Исходиники` и другие имена.

```text
Заказ 12345/
├── 1/
│   └── Исходиники/
│       ├── Макет 1 ....tif
│       └── Макет 2 ....tif
├── 2/
│   └── Исходиники/
│       └── Макет 3 ....tif
└── 3/
    └── Исходиники/
        └── Макет 4 ....tif
```

После рендера Worker создаёт в папке заказа следующую числовую ревизию. Если уже
существуют `1`, `2`, `3`, результат появится в такой структуре:

```text
4/
├── Исходник/
│   └── ЦП Макет N 60х30.jpg
└── Превью/
    └── ЦП Макет N 60х30.jpg
```

В `Исходник` сохраняется готовая ЦП без изменений. В `Превью` сохраняется эта же
ЦП с добавленной снизу белой полосой и чёрной подписью по центру. Текст подписи —
имя JPEG без расширения. Размер в имени берётся из preset и указывается в
сантиметрах. В Core отправляется файл из `Превью`. Повтор того же
`job_id + attempt` использует уже созданные файлы и не создаёт ещё одну ревизию.

При `layout_number: 3` будет выбран файл из `2/Исходиники`; совпадение номера
ревизии с номером макета не требуется.

Во всех подпапках ревизии Worker ищет файлы, имя которых начинается с точной связки
`Макет N`. Граница номера обязательна: `Макет 3` не совпадает с `Макет 30`.
Поиск рекурсивный, если `search.recursive=true`.

Связка `Макет N` должна находиться только в одной числовой папке. Номера макетов
растут по ходу работы над заказом, поэтому Worker не выбирает «самую новую»
копию: он находит единственную папку, содержащую требуемый номер. Если связка
встретилась в двух числовых папках, структура считается неоднозначной и Job
получает `MULTIPLE_FILES_FOUND`.

В найденной папке применяется `extension_priority`. Default содержит только
`.tif` и `.tiff`,
поэтому JPEG/PNG-preview с тем же именем не выбирается. Если эти форматы реально
используются как production source, их нужно явно добавить в preset. Два
равноприоритетных production-файла в найденной папке дают
`MULTIPLE_FILES_FOUND`; случайный первый файл не выбирается.

Если единственный найденный `Макет N` представлен PSD/PSB, Worker возвращает
`UNSUPPORTED_FORMAT`. PSD/PSB пока диагностируются, но не обрабатываются.
`root_priority=true` явно
разрешает порядок roots только для устранения дубликата одной и той же ревизии.

## Bootstrap configuration

| Переменная | Default / назначение |
| --- | --- |
| `PROOF_CORE_URL` | Обязательна: HTTP(S) URL без credentials/query |
| `PROOF_WORKER_TOKEN` | Обязательна: secret из Core |
| `PROOF_WORKER_NAME` | Обязательна: локальное имя в диагностике |
| `WORKER_DATA_PATH` | `/data`: SQLite, manifests, analysis, tmp, logs |
| `WORKER_OUTPUT_PATH` | `/output`: неизменяемые результаты по Job/attempt |
| `WORKER_SOURCE_ROOTS` | `["/sources/main"]`: JSON allowlist mounts |
| `WORKER_LISTEN_HOST` / `WORKER_LISTEN_PORT` | Bootstrap socket подписанного `/wake`; Compose использует `0.0.0.0:8090` |
| `LOG_LEVEL` | `INFO` |
| `HEARTBEAT_INTERVAL` / `POLL_INTERVAL` | `30` / `5` секунд |
| `HTTP_TIMEOUT` | `30` секунд на сетевую операцию |
| `RETRY_INITIAL_SECONDS` / `RETRY_MAX_SECONDS` | `1` / `30`, exponential backoff |
| `STORAGE_RETRY_LIMIT` | `5` повторов недоступного source storage |
| `FILE_NOT_FOUND_RETRY_LIMIT` | `0`, отдельная политика отсутствующего файла |
| `HEALTH_INTERVAL` / `HEALTH_MAX_AGE` | `5` / `90` секунд |
| `VIPS_CONCURRENCY` / `VIPS_CACHE_MEMORY_MB` | `2` / `128` |
| `SOURCE_MAIN_HOST_PATH` | Только Compose: существующий host directory |

Бизнесовые настройки, включая размеры, scoring и поиск, находятся в Preset.
Heartbeat, polling, retries, healthcheck и UNC mappings редактируются отдельно
для каждого Worker в Core UI и применяются после heartbeat. Значения окружения
для них остаются bootstrap fallback до получения первой конфигурации Core.
Технические defaults confidence/thresholds являются начальными настройками MVP;
их нужно оценить на реальных макетах через CLI. Поля и validation описаны в
`app/models/preset.py` и `app/imaging/analysis/config.py`. Выход ограничен
100 мегапикселями; preview — 4096 px по максимальной стороне.

## Recovery и диагностика

Core остаётся источником истины. При старте и после неоднозначной ошибки Worker
делает atomic claim: существующий Core возвращает уже назначенный этому токену
Job. Его `job_id + attempt` сравниваются с локальным журналом. Новые задания не
берутся параллельно текущей обработке. Heartbeat выполняется отдельно от CV/рендера.

JPEG пишется в sibling temporary file, синхронизируется и атомарно переименовывается.
До рендера сохраняется render-plan. После — SHA-256 и artifact manifest, затем
SQLite `READY_TO_UPLOAD`. Поэтому даже падение сразу после rename позволяет
проверить JPEG и продолжить загрузку без чтения source. Размер, SHA-256, RGB,
DPI и декодирование JPEG проверяются при восстановлении.

Перед HTTP upload журналируется намерение. Потерянный ответ повторяется с тем же
файлом и ключом, включающим Job, attempt и SHA-256. Если файл испорчен после
неоднозначной загрузки, Worker сохраняет его и блокирует локальное выполнение
для разбора. Он не создаёт потенциально второй результат. До начала upload
повреждённый файл переименовывается в `result.corrupt-*.jpg` и допускается рендер
заново.

У Core v1 нет Worker GET и серверной проверки expected attempt/lease. Если после
потерянного ответа `complete` Core уже выдал следующий Job, старый локальный
результат остаётся `DETACHED`, без догадок о его глобальном статусе. Полностью
устранить гонки при ручном retry/переназначении активной попытки можно только
изменением контракта Core. Не переиспользуйте один токен на двух машинах.
Подробности: [docs/core-contract.md](docs/core-contract.md).

`SIGTERM/SIGINT` прекращают polling и новые claims. Текущая libvips-операция может
закончить атомарное сохранение; последующие этапы останавливаются. При принудительном
завершении Docker после grace period используется recovery. Heartbeat продолжает
работать, пока выполняется текущая тяжёлая операция.

Healthcheck читает локальный пульс event loop и проверяет процесс. Недоступный
Core сам по себе не делает контейнер unhealthy. Публичного `/health` нет.
JSON logs пишутся в stdout и `/data/logs/worker.jsonl` с ротацией. Важные events и
progress отправляются Core; при сетевой ошибке telemetry остаётся в локальных
логах, а результат сохраняется. У telemetry нет гарантии exactly-once.

`/data/jobs/<hash Job ID>/<attempt>/` содержит `analysis.json`, `render-plan.json`,
`artifact.json`, `upload.json`. `/output/<hash>/<attempt>/result.jpg` хранит результат.
Raw ICC bytes в JSON не попадают. Автоматического удаления готовых или незавершённых
результатов нет: следите за свободным местом и удаляйте только проверенные архивные
попытки по своей retention policy. `/data/tmp` выделен под временные файлы libvips.

## Dev CLI

Без токена и Core, внутри контейнера:

```sh
docker run --rm \
  --mount type=bind,source=/mnt/test-artworks,target=/sources/test,readonly \
  --mount type=volume,source=proof-dev-data,target=/data \
  --mount type=volume,source=proof-dev-output,target=/output \
  bellenne-proof-worker:0.1.0 \
  python -m app.cli process --source /sources/test/example.tif \
  --output /output/test.jpg --data-path /data/dev --diagnostic
```

Опционально `--preset /path/preset.json`. `--diagnostic` сохраняет preview с
отмеченными кандидатами в `/data/dev/diagnostic_preview.jpg`; подробности и TOP
кандидаты — в `diagnostics.json`. Диагностическое изображение не является Result
и не отправляется в CRM.

Локально на Windows для разработки:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install pyvips-binary
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m app.cli process --source dev-data/example.tif --output dev-data/proof.jpg --data-path dev-data/work --diagnostic
```

`pyvips-binary` нужен только для удобной установки libvips на dev-машине.
Production Docker использует системный libvips. Реальные макеты храните в
игнорируемой Git папке `dev-data/`.

## Тесты

```sh
python -m pytest -q
python -m ruff check app tests
docker build --target test -t bellenne-proof-worker:test .
docker run --rm bellenne-proof-worker:test
```

Тесты генерируют изображения во временных каталогах: геометрия и координаты,
scorers, NMS/confidence, поиск и ошибки storage, native color headers, RGB/ICC,
RGBA и 16-bit, рамки, DPI/JPEG, большой BigTIFF, atomic save, journal recovery,
потерянные upload/completion acknowledgements, отдельные попытки и heartbeat во
время обработки. Тесты HTTP-контракта используют MockTransport и не обращаются
к production Core.

Опциональная проверка с исходниками вашего Core запускает отдельную копию
серверного приложения на loopback с временной SQLite и отключённой CRM.
Она проверяет полный pipeline, потерянное подтверждение upload и восстановление
без source: на сервере остаётся ровно один Result.

```powershell
py -3.12 -m venv dev-data/core-venv
.\dev-data\core-venv\Scripts\python.exe -m pip install -r D:/Works/Python/BellenneOne/apps/proof/requirements.txt
$env:PROOF_CORE_SOURCE = 'D:/Works/Python/BellenneOne/apps/proof'
.\.venv\Scripts\python.exe -m pytest tests/test_live_core.py -q
```

На Linux используйте `dev-data/core-venv/bin/python` и `export PROOF_CORE_SOURCE=...`.
Другой interpreter можно указать через `PROOF_CORE_PYTHON`.
Production БД, `.env` и серверные исходники эта проверка не изменяет.

## Troubleshooting

| Событие | Что проверить |
| --- | --- |
| `INVALID_CONFIG`, HTTP 401/403 | Токен нужного Worker, URL модуля, schema preset |
| `SOURCE_STORAGE_UNAVAILABLE` | Host mount, SMB, права и storage marker; файл не объявляется отсутствующим |
| `FILE_ACCESS_DENIED` | Права чтения mount для UID 10001 |
| `FILE_NOT_FOUND` | Артикул, roots и выбранные стратегии |
| `MULTIPLE_FILES_FOUND` | Устраните дубль либо явно настройте приоритет |
| `UNSUPPORTED_COLOR_SPACE` | Нужен RGB/RGBA source; автоматической CMYK-конвертации нет |
| `ICC_PROFILE_MISSING` | Допустимый warning; файл не получает выдуманный профиль |
| `SOURCE_TOO_SMALL` | Исходник должен быть не меньше заданного crop в пикселях |
| `ANALYSIS_NO_VALID_CROP` / `LOW_CONFIDENCE` | Посмотрите CLI diagnostics и настройте scoring на макетах |
| `OUTPUT_WRITE_ERROR` | Место на диске, права `/data` и `/output` |
| `CORE_UNAVAILABLE` / `UPLOAD_ERROR` | Сеть и Core; сохранённый Result остаётся на диске |
| `JOB_STATE_ERROR`, `BLOCKED` | Сверьте назначение/attempt в Core; локальный результат сохранён |
| `DETACHED` | Core больше не выдаёт эту попытку; проверьте Result в Core до ручного удаления |

Перед первым производственным использованием прогоните репрезентативные реальные
макеты через CLI и сравните выбранные фрагменты с ожиданиями печатника.
