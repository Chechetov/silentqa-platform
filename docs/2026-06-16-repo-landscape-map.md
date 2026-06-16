# Карта репозиториев и работы (2026-06-16)

Аудит всех папок, связанных с записью/QA-платформой. Read-only разведка 4 агентами.
Цель: не потерять работу, разбросанную по папкам; понять, что брать за основу универсальной системы.

## TL;DR — главный вывод (меняет постановку)

**«Универсальную систему на основе realestate» строить не нужно — она уже существует.** Это `silentqa` / `meet-mt`.

- `realestate` = полный QA-движок (пайплайн, quality, extract, AmoCRM, KZ-socks) **без** мультитенантности.
- `silentqa` = **тот же движок** + мультитенантность (схема-на-тенанта), платформенная админка, user-auth + роли (admin/viewer/manager), provisioning. Phase 1 уже перенёс движок целиком.
- Значит работа = **доуниверсализировать silentqa**, а не строить заново на realestate.

## Карта папок

| Папка | Что это | Роль | Бэкап off-box |
|---|---|---|---|
| `/root/projects/realestate` (+ `/home/dev`) | Прод Рогов-недвижимость, :8002 | 🔴 ЖИВОЙ ПРОД | ✅ GitHub `Chechetov/realestate-analytics` |
| `/root/projects/meet` | Live Meeting Recorder :8005; держит ветку `multi-tenant-core-phase1` | 🔴 прод + «origin» для silentqa | ❌ **нет remote вообще** |
| `/root/projects/meet-mt` | git-worktree ветки `multi-tenant-core-phase1` — **dev-воркспейс платформы** | 🟢 разработка | ❌ только локально |
| `/root/projects/silentqa` | Прод мультитенант-платформа :8007; origin = `/root/projects/meet` (локально!) | 🟢 **= универсальная система** | ❌ **только этот бокс** |
| `/root/projects/dental` (+ `/home/dev`) | Прод FullDent (старый), fulldent.clinicpush.com :8004 | 🔴 прод (юзер сказал — можно снести) | ? |
| `/root/projects/fillers` (+ `/home/dev`) | Прод filler-клиент, calls.automate-it.fun :8001 | 🔴 прод | ? |
| `/root/projects/voiceqa` | Замороженный ранний прототип той же платформы (Docker, Claude API, Whisper-local) | ⚪️ архив | ✅ GitHub `Chechetov/voiceqa` |
| `/root/projects/meet-build` | НЕ git. rsync-клон + сборка `Meeting Recorder 1.0.0.exe` + миграция 011 | ⚪️ скретч-сборка | ❌ |
| `/root/projects/rogov` | НЕ git. скретч; канон под `/home/dev/projects/rogov/*` | ⚪️ | — |
| `/home/dev/projects/rogov/rogov-partner-portal` | Живой партнёр-портал (нормализация AmoCRM, скоринг) | 🔴 | ⚠️ **10 коммитов не запушено** |
| `/home/dev/projects/rogov/rogov-estate-astro` | Сайт Рогов-эстейт | 🔴 | ⚠️ ветка `client-edits-2026-06-15` не запушена |
| `/home/dev/projects/PBN` | Lead-сеть | 🔴 | ⚠️ 5 коммитов + ветка не запушены |
| `/root/projects/chechetov.com` | Только бриф (Next.js личный сайт), кода нет | ⚪️ | — |

## Приложения-рекордеры: поколения дизайна

| Gen | Где | Что |
|---|---|---|
| Gen 1 (v3.0.0, апр) | `/home/dev/.../realestate/desktop-app` (stale) | Старый UI, Basic-auth, 434-строчный renderer |
| **Gen 2 (v4.0.0, `bfdd6bd`, 14 мая)** | `realestate dev`, `meet main` | **«Новый дизайн»**: тёмная тема, безрамочный titlebar, поток Setup→Login→Recorder, чанковая загрузка, ретраи, анимации. renderer 846 строк |
| **Gen 3 (`cc16418`, 12 июня)** | **`meet-mt`, `silentqa`** (байт-в-байт) | Gen 2 + `X-API-Key` (мультитенант). renderer 859 строк. **Самая свежая пригодная сборка** |

Хвосты по приложениям:
- **Mac `call-recorder-5.0.6-x64.dmg` (7 июня, подписанный)** опубликован на realestate, но **исходника нет ни в одном репо** (собран на маке, залит напрямую). Репо знают только v4.0.0.
- `meet-build/Meeting Recorder 1.0.0.exe` (8 июня) — на базе Gen 2 (без X-API-Key), вне git.
- В `electron-builder.yml` silentqa всё ещё `productName: Call Recorder` — брендинг SilentQA только на странице загрузки.
- Расширения Chrome/Yandex (Rogov Recorder 1.3.0) — popup-UI не редизайнили, только добавили скрытый input под API-key.

## ⚠️ Работа под риском (приоритет бэкапа)

1. **🔴🔴 ВСЯ ПЛАТФОРМА (`meet`/`meet-mt`/`silentqa`, ветка `multi-tenant-core-phase1`) — нет off-box бэкапа.** Живёт только на этом боксе. Один сбой диска = полная потеря самой ценной работы. → запушить на приватный GitHub (классификатор блокирует меня; владелец запускает руками).
2. **rogov-partner-portal** — 10 коммитов не запушено + 5 файлов незакоммичено.
3. **rogov-estate-astro** — ветка `client-edits-2026-06-15` + фича брошюры не запушены.
4. **realestate stash@{0}** (на `main`) — 9 файлов / 566 строк: INSTRUCTIONS, auth-заголовок расширения, `_get_audio_duration`, mic-permission страницы. Никогда не коммитилось.
5. **Mac 5.0.6 dmg** — исходник отсутствует в git.
6. **meet-build/pending-migrations/011_…responsible_user_id.py** — фикс schema-drift отдельным файлом вне git.
7. **dental** (`/root` и `/home/dev`) — `_get_audio_duration` в pipeline.py незакоммичен.
8. **fillers/uiscom_loader.py** — KZ-прокси роутинг (~40 строк) незакоммичен (только /root).
9. **PBN** — 5 коммитов + ветка не запушены.

## Состояние универсализации: что есть / что net-new

**Уже готово (generic):**
- Мультитенант-инфра live (`silentqa.com`, тенанты fulldent/shuravin): схемы-на-тенанта, роутинг по поддомену, auth+роли, платформ-админка, provisioning CLI. 81+ тестов зелёные.
- Config-driven домены: `companies/<id>.json` (`word_boost`, `protocol`, `scenarios[]`, `criteria[]`). Уже работает для realestate, dental, cosmetics/fillers, default — **4 домена параметризованы JSON-ом, не хардкодом**.
- Движок extraction (`extract.py`) **полностью доменно-агностичен**: prompt + JSON Schema → structured output. Сама идея «базы знаний» = `extraction_templates + complexes + complex_extractions`.

**Есть спекой, не реализовано:**
- Phase 2 (per-tenant config + реестр интеграционных адаптеров: Port 1 ingest, Port 2 CRM-push) — спека `2026-06-10-multi-tenant-core-design.md §8`, кода нет.
- Унификация рекордер-identity (брокер→user-token для не-AmoCRM тенантов) — спека `2026-06-14-unify-recorder-identity-design.md` (только в meet-mt), не реализована.

**Net-new для «универсальный дашборд + база знаний»:**
1. **«База ЖК» → generic «База знаний»**: таблица `complexes` → конфигурируемое имя сущности; nav-label и роуты; нормализатор `complex_match.py` (срезает «жк», «жилой комплекс», «мфк») → per-template/per-tenant. Сейчас даже в silentqa SPA пункт зовётся «База ЖК».
2. **Per-tenant имя сущности базы знаний** (стоматология → «Пациенты»/«Болезни»/«Методы», филлеры → «Бренды») — net-new.
3. **Убрать Rogov-специфичные сиды шаблонов** из миграций (005/008: «Звонок брокера», «Zoom-встреча брокера (презентация ЖК)»); `sessions.py:123 _DEFAULT_DESKTOP_TEMPLATE_NAME` захардкожен под Rogov Zoom-шаблон.
4. **Генерализация дашборда** — прятать RE-специфичные страницы по тенанту; аналитика (managers, broker scorecard) заточена под realestate/AmoCRM.
5. **Хардкод-блокеры** (агент D): жанр Zoom по точной строке `ZOOM_MEETING_TEMPLATE_NAME` `pipeline.py:213` → по метаданным шаблона; `scenario_id="outbound_residential"` + `company_id="realestate"` в `amocrm_poll` → из per-tenant конфига.
6. **Донести из realestate в платформу:** ветка жанра Zoom (`is_zoom_meeting` + чек-лист-нота + summary-guard) — не перенесена; `routes/webhooks.py` — отсутствует в платформе; богатый download-хаб / `recorder.html`.
