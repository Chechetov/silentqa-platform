# Dental MVP на SilentQA (тенант fulldent) — дизайн (вариант A, generic + config-driven)

**Дата:** 2026-06-17
**Статус:** дизайн на ревью (перед планом реализации)

**Цель.** На тенанте **fulldent** мультитенантной платформы **SilentQA** оценивать записанные приёмы у стоматолога и выдавать, помимо транскрипта: (1) структурную **«Карту приёма»** — пациент / анамнез / диагноз / план лечения / стоимость / **зубная формула**, и (2) **дентальную QA-оценку коммуникации** врача.

## Продуктовая рамка (определяет архитектуру)

SilentQA — **один мультитенантный движок, много вертикалей**: стоматология, салон красоты, агентство недвижимости — каждая это **отдельный тенант со своим поддоменом** (`<slug>.silentqa.com`). Поэтому:

- Новые возможности — **generic + config-driven**, параметризуются через `companies/<tenant>.json`. **Доменная специфика живёт только в конфиге тенанта, не в коде движка.**
- «Карта приёма» реализуется как **общая фича `card_extraction`** (структурная выжимка по схеме из конфига). Дентальность = контент конфига fulldent (промпт + json-схема с зубами). Салон красоты позже заведёт свою карту тем же механизмом.
- **Безопасность общего кода** (ключевое требование): правки движка — generic и **гейтятся наличием конфиг-блока у тенанта**; тенант без блока (realestate) поведение не меняет, и это **покрыто тестом**. Так совмещаем «движок = общий SilentQA» и «не сломать другие тенанты». Отдельный репозиторий не нужен.

## Архитектура и поток

```
upload → merge (ffmpeg → wav 16k) → ASR (per-tenant движок; для fulldent: ElevenLabs Scribe v2, ru, diarize)
  → [диаризация пропускается: ASR вернул спикеров] → sentiment
  → quality (QA по сценарию тенанта) → card_extraction (если есть в конфиге тенанта)
  → save (transcript.json, quality.json, card.json) → дашборд: транскрипт + QA + карточка карты
```

Решения (обоснование — [[dental-asr-comparison]] + валидация на реальной записи):
- **ASR per-tenant из конфига.** fulldent: **ElevenLabs Scribe v2** (осн.) → **AssemblyAI universal-2** (фолбэк) → Whisper large-v3. Эмпирически Scribe самый полный/чистый для русского (1420 слов, 92% покрытие, 0 петель, диаризация, 34с).
- **`card_extraction`** — generic: `{prompt, json_schema}` из конфига → gpt-5.4 strict structured output, **без** связки с `complexes`/`match_or_create_complex`.
- **QA scenario-aware + `N/A`** — generic: критерии из сценария конфига; неприменимые критерии = `null`, исключаются из `overall_score`.

## Компоненты

### 1. `companies/dental.json` (конфиг тенанта; платформа SilentQA)
- `id:"dental"`, `name`, `language:"ru"`
- `asr`: `{ engine:"elevenlabs", word_boost:[...82 дентальных термина...] }` (word_boost → keyterms ElevenLabs)
- `quality`: `scenarios[]`+`criteria[]` (готовые `consultation`/`treatment_plan`/`primary_call`/`confirmation_call` из легаси-конфига) + `protocol`
- `card_extraction`: `{ label:"Карта приёма", prompt:"<промпт>", json_schema:{<схема ниже>} }`

fulldent → `shared.tenants.company_config_id="dental"`.

### 2. `worker/tasks/transcribe.py` — ASR-адаптер ElevenLabs (generic)
`_transcribe_elevenlabs(audio_path, keyterms=None)`: `POST api.elevenlabs.io/v1/speech-to-text`, `model_id=scribe_v2`, `language_code=ru`, `diarize=true`, ключ из env `ELEVENLABS_API_KEY`. Ответ word-level `words[]` со `speaker_id` → склейка в реплики `{speaker,start,end,text}`. Диспетчеризация `transcribe_audio` по `engine` (`elevenlabs|assemblyai|whisper`) с цепочкой фолбэка; engine — из конфига тенанта (`asr.engine`), дефолт из env.

### 3. `worker/tasks/card.py` (НОВЫЙ, generic) — структурная карта
`run_card_extraction(session_id, cfg) -> dict|None`: нет `cfg["card_extraction"]` → `None` (no-op). Иначе читает `transcript.json`, плоский текст со спикерами, OpenAI `gpt-5.4` `responses.create` strict по схеме из конфига (паттерн `extract.py`, но **схема/промпт из конфига, без DB-шаблона и без `match_or_create_complex`**) → `results/<session_id>/card.json`. Мягкая деградация (ошибка → лог + `None`).

### 4. `worker/tasks/pipeline.py` — врезка
После quality: `cfg = tenant_company_config(); if cfg.get("card_extraction"): run_card_extraction(session_id, cfg)`. **Config-gated** → realestate не затрагивается. Точные строки — в плане.

### 5. `worker/tasks/quality.py` — scenario-aware + N/A (generic)
Критерии из матча сценария конфига; разрешить `score=null` (N/A) для неприменимых, исключать из `overall_score`. Config-gated (для realestate поведение прежнее).

### 6. Backend + дашборд (generic)
- `GET /api/sessions/{id}/card` → `card.json` (404 если нет; гейт по наличию `card_extraction` в конфиге).
- `index.html`+`app.js`: карточка структурной карты на странице звонка, **заголовок из `card_extraction.label`**, прятать если карты нет. Generic — рендерит схему карты любого тенанта.

### 7. Окружение / провижининг
`ELEVENLABS_API_KEY` в prod `.env` (только env, не в репо). fulldent → `company_config_id=dental`.

## Схема дентальной «Карты приёма» (в `dental.json` → `card_extraction.json_schema`, strict)

```
clinical:
  patient: { name, age, gender, phone }                  # nullable
  chief_complaint: string|null
  anamnesis: { complaints[], history, allergies[], chronic_conditions[], medications[] }
  examination: string[]
  dental_status[]: { tooth, status, note }               # ЗУБНАЯ ФОРМУЛА: номер зуба + статус по зубу
  diagnosis: string[]
  treatment_plan:
    stages[]: { procedure, teeth, priority(срочно|планово|по желанию|не указано), timeline, price }
    total_cost: string|null
    payment_options: string[]
  recommendations: string[]
  visit_outcome: { result(согласие на лечение|думает|повторный приём|направление|отказ|не указано), next_step }
  summary: string
qa:
  scenario(consultation|treatment_plan|other), overall_score(0–10),
  criteria[]: { id, name, score(0–10 | null=N/A), comment },
  protocol_missed[], improvement_suggestions[], key_moments[]: { time_sec, type, description }
speaker_roles[]: { speaker, role(врач|пациент|ассистент|администратор|другое), name }
```
Новое vs валидированная схема: **`dental_status[]`** (номера зубов + статус по каждому зубу — нотация в промпте, напр. FDI/универсальная; `status` — строка: здоров/кариес/пломба/коронка/имплант/удалён/в лечении/…). Остальные дентальные поля добавим, когда наберётся объём данных. Базовый strict-вариант — в `/tmp/dental-mvp/dental_eval_lib.py`. Правило промпта: ничего не выдумывать — нет данных → `null`/пустой список.

## Тестирование (юнит, без сети/БД)
- `transcribe`: парсинг ответа ElevenLabs (фикстура `words[]`→реплики); фолбэк elevenlabs→assemblyai.
- `card`: `run_card_extraction` с замоканным OpenAI → `card.json`; **нет `card_extraction` в конфиге → `None` (no-op)**.
- `pipeline`: карта вызывается только при наличии `card_extraction`; **realestate-конфиг → не вызывается** (регресс-страховка общего кода).
- `quality`: N/A-критерий исключается из overall; realestate-конфиг → прежнее поведение.
- backend: `GET /card` 404 без файла; гейт по конфигу.

E2E (на проде): прогнать реальные записи на fulldent → транскрипт + QA + карта в дашборде.

## Деплой
1. Реализация+тесты в `silentqa-dev`, коммит. 2. Деплой кода + `companies/dental.json` на прод SilentQA (`/root/projects/silentqa`, :8007). 3. `ELEVENLABS_API_KEY` в prod `.env`. 4. fulldent → `company_config_id=dental`. 5. Рестарт worker+backend; прогон как пользователь fulldent.

## Риски / вне scope
- **Общий движок** деплоится на прод с realestate → все правки **generic + config-gated + тест «realestate не активирует фичу»**. Это и есть способ совместить «общий движок SilentQA» с «не сломать тенанты».
- **ASR n=1**: Scribe подтверждён на одной (трудной) записи → догнать валидацию на 2–3 реальных.
- **PHI**: аудио уходит в ElevenLabs+OpenAI (модель продукта); согласие/ДПА — отдельно.
- **Реестр пациентов / cross-session — ВНЕ scope.** Это **отдельный проект пользователя (CRM для стоматологии)**, готовится параллельно; наш MVP — карта пер-сессия (`card.json`) + точка интеграции на будущее. **Впоследствии проекты объединяем.**
- Вне scope MVP: телефония/CRM-интеграции, конфиги других вертикалей (заведут позже тем же механизмом).
- ElevenLabs-ключ из чата **ротировать**; ключи провайдеров — только env.
