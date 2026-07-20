# Спека: браузерный рекордер, закрытые загрузки, пер-тенантные API-ключи

- **Дата:** 2026-07-20
- **Статус:** утверждена пользователем (устно в сессии, «делаем всё»)
- **Ветка:** `multi-tenant-core-phase1`

## Зачем

Четыре запроса владельца платформы:

1. Скачивание приложений (`/download`) должно быть доступно только после входа — «регистрацию выдаю я сам», публичного самозаписывания нет и не появляется.
2. Запись «по ссылке в браузере» после авторизации — как `recorder.html` в realestate, но с опциональным звуком вкладки (Zoom/Meet в браузере).
3. Пер-тенантные API-ключи (OpenAI, AssemblyAI, ElevenLabs) с фоллбеком на глобальные из `.env`; редактируют и платформенный админ, и админ тенанта.
4. MacBook: оформить и проверить существующие артефакты (arm64 `.dmg` + расширение), Intel-сборку не делаем.

Попутный фикс: `https://www.silentqa.com` не открывается — on-demand TLS спрашивает `/api/tenancy/domain-check`, который отвечает 404 на `www`, и Caddy не выпускает сертификат.

## Вне объёма

- Telegram-алерты рисковых звонков (ждём токен бота и chat_id от владельца).
- Company-config для тенанта `shuravin` (нужны вводные по клиенту).
- Intel/universal сборка macOS, Safari-расширение.
- Валидация введённых ключей пробным запросом к провайдеру (можно добавить позже).
- `HF_TOKEN` (диаризация) остаётся глобальным — инфраструктурный, не биллинговый.

## A. `/download` за логином + www-редирект

**Сейчас:** весь `backend/static/` смонтирован `StaticFiles(html=True)` в корень (`main.py:137`), `/download/` и бинарники в `/download/dl/*` публичны.

**Дизайн:** явные роуты (новый `backend/app/routes/downloads.py`), объявленные до статик-маунта, перехватывают префикс:

- `GET /download` → 307 на `/download/`;
- `GET /download/` → проверка доступа → `FileResponse(static/download/index.html)`;
- `GET /download/dl/{filename}` → проверка доступа → `FileResponse`; `filename` обязан существовать в каталоге `static/download/dl/` (сравнение по списку файлов каталога, никакой интерполяции пути — защита от traversal).

**Проверка доступа** (`require_download_access`): на тенантном контуре — валидная кука `sqa_session` (любая роль: admin/viewer/manager); на платформенном контуре — валидная `sqa_admin`. Неавторизованный → 302 на `/` (там форма входа соответствующего контура). Файлы физически остаются в `static/download/` — процесс сборки, который кладёт артефакты в `dl/`, не меняется; роуты перекрывают маунт, потому что регистрируются раньше него.

В навигацию дашборда (`index.html`/`app.js`) добавляется пункт «Скачать приложения» → `/download/` (виден всем ролям).

**www-фикс (инфра, вне репо):** в `/etc/caddy/Caddyfile` явный блок перед wildcard-блоком:

```
www.silentqa.com {
	redir https://silentqa.com{uri} permanent
}
```

Явный хост берёт обычный ACME-сертификат (как staging-блоки) и перекрывает `*.silentqa.com`/on-demand по специфичности. Применение: `caddy reload`.

## B. Страница записи `/record` (микрофон + звук вкладки)

**Файл:** `backend/private/record.html` — самодостаточная страница (HTML+CSS+JS в одном файле, по образцу realestate `recorder.html`, стилистика дашборда SilentQA). Каталог `backend/private/` **не** попадает в статик-маунт; страницу отдаёт роут `GET /record` (в том же `downloads.py`) после той же проверки `require_download_access`, но только на тенантном контуре (на платформенном контуре запись не имеет смысла — 404).

**Захват звука:**

- Микрофон: `getUserMedia({audio: {echoCancellation, noiseSuppression}})`.
- Переключатель «+ звук вкладки»: `getDisplayMedia({video: true, audio: true})` (Chrome требует video в запросе; видеотрек сразу останавливаем, оставляем аудио). Пользователю подсказка: выбрать вкладку и включить «Также предоставить доступ к аудио вкладки».
- Смешивание: `AudioContext` → два `MediaStreamAudioSourceNode` → общий `MediaStreamAudioDestinationNode`; `MediaRecorder(destination.stream, audio/webm;codecs=opus)`.
- Деградация: если `getDisplayMedia`-аудио недоступно (Safari, отказ пользователя, вкладка без аудио) — предупреждение на странице и запись только микрофона; запись не прерывается.
- Если пользователь останавливает шаринг вкладки посреди записи — запись продолжается с одним микрофоном, индикатор меняется.

**Отправка:** существующий чанк-протокол без изменений — `POST /api/sessions` (metadata: `source: "web-recorder"`, `employee`, `client_company`/комментарий) → `POST /api/sessions/{id}/chunks` каждые 10 с (ретрай неудавшегося чанка как в realestate) → `POST /api/sessions/{id}/finish`. Авторизация — сессионная кука (same-origin fetch), `require_ingestion_auth` уже принимает валидную сессию. Поле «Сотрудник» префиллится из `GET /api/auth/me` (`employee_name`) и для роли `manager` не редактируется — иначе менеджер потеряет собственный звонок из своей выборки (`employee_scope`).

**После завершения:** страница показывает статус обработки (поллинг `GET /api/sessions/{id}` как в realestate) и ссылку «Открыть в дашборде».

В навигацию дашборда добавляется пункт «Запись» → `/record`.

## C. Пер-тенантные API-ключи

**Хранение.** Shared-миграция `s005_tenant_api_credentials`: `ALTER TABLE shared.tenants ADD COLUMN api_credentials JSONB NOT NULL DEFAULT '{}'`. Формат значения: `{"openai": "<fernet-token>", "assemblyai": "...", "elevenlabs": "..."}` — только эти три провайдера (белый список `PROVIDERS`), значения зашифрованы.

**Крипто:** новый `tenancy/secrets.py`:

- ключ Fernet выводится детерминированно: `urlsafe_b64encode(sha256(SECRET_KEY + ":api-credentials").digest())`;
- `encrypt_credential(str) -> str`, `decrypt_credential(str) -> str | None` (битый токен → `None` + warning, не исключение);
- зависимость `cryptography` добавляется в `worker/requirements.txt` явно (в backend уже приходит через `python-jose[cryptography]`, добавить явно тоже — прямая зависимость не должна быть транзитивной).

**Чтение (резолвер «тенант → env»):**

- Воркер: `worker/tasks/tenant_keys.py` → `api_key(provider: str) -> str | None`: берёт текущий тенант из contextvar, читает `api_credentials` через `tenancy/registry.py` (в выборку реестра добавляется колонка; кэш там уже есть), расшифровывает; если пусто/нет — `os.getenv(ENV_NAME[provider])`. Точки замены: `quality.py:659`, `quality.py:1081`, `coaching.py:216`, `transcribe.py:88` (assemblyai), `transcribe.py:151` (elevenlabs).
- Бэкенд: аналогичный хелпер `backend/app/tenant_keys.py`, читающий `request.state.tenant` (строка реестра; туда добавляется `api_credentials`) — используется в `eval_prompt_rewrite.py:60`.
- Инвариант: расшифрованный ключ живёт только в локальной переменной вызова; в логи, ответы API и отчёты не попадает.

**API записи/чтения (маскированное):** семантика общая для обоих уровней:

- `GET` → `{"openai": {"set": true, "last4": "abcd"}, "assemblyai": {"set": false}, ...}`;
- `PUT` с телом `{"openai": "sk-..."}` — задать; `{"openai": ""}` — удалить (возврат на глобальный ключ); отсутствующий в теле провайдер не трогается; неизвестный провайдер → 422;
- после записи — `registry.invalidate()`.

Эндпоинты:

- платформенный: `GET/PUT /api/platform/tenants/{slug}/keys` (в `platform_tenants.py`, под `require_platform_admin`);
- тенантный: новый `backend/app/routes/integrations.py`, `GET/PUT /api/integrations/keys` под `require_admin`.

**UI:**

- платформенная админка (`admin.html`/`admin.js`): блок «API-ключи» в карточке тенанта — три поля write-only с индикацией «задан …abcd», кнопка «Сохранить», кнопка «Сбросить» на провайдер;
- дашборд (`index.html`/`app.js`): новая страница `#integrations` «Интеграции», видна только роли admin (`data-admin-only`), те же три поля. Подпись: «Если ключ не задан — используется общий ключ платформы».

## D. MacBook: оформление загрузок

В `static/download/index.html`: у `.dmg` подпись «для Apple Silicon (M1–M4)»; блок «Установка на Mac»: первый запуск через правый клик → «Открыть» (Gatekeeper, приложение без нотаризации), и при необходимости `xattr -cr /Applications/SilentQA\ Recorder.app`; шаги установки Chrome-расширения на Mac (распаковать zip → `chrome://extensions` → режим разработчика → «Загрузить распакованное»). Проверка: скачивание всех пяти артефактов через Caddy (HTTP 200, размер совпадает с файлом на диске).

## Тесты

По образцу существующих (Starlette TestClient + FakeRedis, без Postgres/Redis; `cd backend && python -m pytest tests/`, `cd worker && python -m pytest`):

- **Гейт загрузок/записи:** аноним на `/download/`, `/download/dl/x`, `/record` → 302 на `/`; пользователь с сессией → 200; платформенный админ на платформенном контуре → 200 для `/download/`, 404 для `/record`; несуществующий файл в `dl/` → 404; попытка traversal (`..%2f`) → 404.
- **API ключей:** PUT сохраняет шифротекст (в моке БД значение ≠ открытому ключу и расшифровывается обратно), GET маскирует (`set`/`last4`, полного ключа нет в ответе), пустая строка удаляет, неизвестный провайдер → 422, viewer/manager → 403 на тенантном эндпоинте.
- **Резолвер воркера:** с замоканным реестром — тенантный ключ побеждает env; без тенантного — env; битый шифротекст → фоллбек на env + warning.
- **secrets:** encrypt→decrypt roundtrip; decrypt мусора → None.
- Существующие тесты (включая `test_no_raw_connects.py`) остаются зелёными.

Ручная проверка на staging (:8008): запись микрофон+вкладка в Chrome, деградация в Safari, звонок доходит до `completed`, менеджер видит свой звонок.

## Выкладка (прод)

1. `cd backend && python -m app.migrate` (применит `s005` ко всем схемам; shared-миграция).
2. Рестарт `silentqa-backend`, `silentqa-worker`, `silentqa-worker-io`.
3. Правка `/etc/caddy/Caddyfile` (www-блок) + `caddy reload`.
4. Верификация: `curl -I https://www.silentqa.com` → 301 на apex; `/download/` без куки → 302; запись на staging.

Откат: `git revert` + рестарт; колонка `api_credentials` обратной миграции не требует (DEFAULT '{}', старый код её не читает).
