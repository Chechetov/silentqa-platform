# silentqa.com — клиентские поддомены и деплой платформы (Plan 3a)

*Дата: 2026-06-12. Статус: одобрено устно (KZ-relay, отдельный деплой, on-demand TLS, CLI-провижининг).*

## 1. Цель

Каждый клиент платформы получает адрес `https://<slug>.silentqa.com` — это
одновременно и дашборд, и сервер для recorder-клиентов (один origin: SPA +
`/api/*`). Заведение клиента — одна CLI-команда, без ручных шагов на DNS,
relay или Caddy. Первые два клиента: `fulldent`, `shuravin`.

## 2. Ключевые решения

| Решение | Выбор | Почему |
|---|---|---|
| Путь трафика | KZ-relay (89.207.255.231) → Hetzner | клиенты в основном РФ; relay — проверенный путь; **проверено 2026-06-12: relay = тупой TCP-passthrough 80/443 без SNI-фильтра** — на relay ничего делать не нужно |
| DNS | Cloudflare (зона уже там), **серая тучка**: `*.silentqa.com` и `silentqa.com` → A 89.207.255.231 | один wildcard навсегда; оранжевая тучка не подходит (TLS должен терминироваться на нашем Caddy, путь РФ→CF менее предсказуем) |
| TLS | **On-demand TLS** + `ask`-эндпоинт | стоковый Caddy 2.6.2 без CF-модуля → wildcard DNS-01 потребовал бы пересборки бинаря под 4 живых проекта; on-demand выпускает серт при первом заходе, гейт по реестру тенантов |
| Платформа | **Отдельный чистый деплой** `/root/projects/silentqa`, порт **8007** (8006 занят сторонним server.py), БД `silentqa` (PG 5432), Redis `6379/4`, юниты `silentqa-{backend,worker}.service` | realestate (Рогов) — готовый клиентский проект, НЕ трогаем; meet — песочница разработки, остаётся |
| Код | ветка `multi-tenant-core-phase1` (Plan 1 + Plan 2) | на свежей БД cutover S002 = no-op; grace/api_key_required=false не нужны — новые тенанты сразу строгие |
| ML-модели | общий кеш `/root/.cache/huggingface` (3 ГБ: faster-whisper-large-v3, rubert) | всё под root — новый деплой подхватывает кеш автоматически, повторная загрузка не нужна; в юнитах HOME не переопределять |
| Провижининг | CLI `python -m app.provision_tenant` | клиентов единицы; веб-админка на admin.silentqa.com — следующий план |

## 3. Новый код (единственный): `GET /api/tenancy/domain-check`

Гейт для `on_demand_tls ask`. Контракт:
- `?domain=<host>` → **200**, если host = `BASE_DOMAIN`, `admin.BASE_DOMAIN`,
  `<slug>.BASE_DOMAIN` активного тенанта или custom_domain активного тенанта;
  иначе **404**.
- Открытый, работает на платформенном контуре (Caddy ходит на
  `http://localhost:8007` без тенантного Host), без БД-запроса на каждый хит —
  через `TenantRegistry` (TTL-кеш 30с).
- Кейсы: неизвестный поддомен → 404 (LE-серт не минтится), suspended → 404,
  мульти-уровневый поддомен (`a.b.silentqa.com`) → 404.

## 4. Caddy

Глобально (в существующий блок опций):
```
on_demand_tls {
    ask http://localhost:8007/api/tenancy/domain-check
}
```
Сайт:
```
silentqa.com, *.silentqa.com {
    encode gzip zstd
    tls { on_demand }
    reverse_proxy localhost:8007
}
```
Всегда: `caddy validate` перед `systemctl reload caddy`.

## 5. Деплой (состав)

1. `git clone /root/projects/meet /root/projects/silentqa && git checkout multi-tenant-core-phase1`; свой `.venv` (backend+worker requirements).
2. `createdb silentqa` (peer postgres). `.env`: новый SECRET_KEY, `DATABASE_URL*` → silentqa, `REDIS_URL=redis://localhost:6379/4`, `BASE_DOMAIN=silentqa.com`, `DEFAULT_TENANT=` (пусто), API-ключи (AssemblyAI/OpenAI/HF) — те же, что у meet/realestate, пути хранилища `/root/projects/silentqa/data/*`.
3. Юниты по образцу meet-*: WorkingDirectory/EnvironmentFile/PATH → silentqa, uvicorn `--port 8007`, worker `-Q default,transcription` (+`-B`, как у прод-воркеров с Beat). enable+start.
4. Старт backend сам гонит `app.migrate`: S001 (реестр) → S002 (no-op на свежей БД) → тенант-трек 001–012 для нуля тенантов (т.е. только shared).
5. Caddy-блок + DNS (п.4, п.2 таблицы).
6. Провижининг `fulldent`, `shuravin` (admin-email владельца, сгенерированные пароли, display-name уточняются позже — поле в shared.tenants правится UPDATE'ом).
7. Smoke: domain-check 200/404; `https://fulldent.silentqa.com` — серт выпускается, SPA-логин работает, viewer/admin-матрица живая, `POST /api/sessions` без ключа → 401 (строгий режим), с ключом → 201; изоляция: сессия fulldent не видна shuravin.

## 6. Онбординг клиента (после этого деплоя)

```
cd /root/projects/silentqa/backend && ../.venv/bin/python -m app.provision_tenant <slug> \
  --name "<Имя>" --admin-email <email> --admin-password <пароль>
```
Клиенту отдаются: `https://<slug>.silentqa.com` (дашборд + сервер для рекордеров), логин/пароль админа, API-ключ `sqa_...` (печатается один раз). Сертификат выпустится при первом заходе (~2-3 сек). Ничего больше настраивать не нужно.

## 7. Безопасность

- Минт сертов гейтится `ask` → только зарегистрированные хосты (нет abuse LE-лимитов; сами лимиты ~50 новых сертов/нед на домен — запас на десятки клиентов).
- Новые тенанты: `api_key_required=true` с рождения; изоляция данных/сессий/файлов — из Plan 1/2.
- Relay не хранит TLS-ключей (passthrough), терминация только на Hetzner.

## 8. Вне скоупа

Веб-админка admin.silentqa.com (следующий план); лендинг на apex; перевод realestate/meet в тенанты платформы; ротация API-ключей через CLI-флаг.

## 9. Открытая зависимость

DNS-записи в Cloudflare добавляются владельцем вручную (API-токена на сервере нет) ЛИБО передаётся scoped-токен (Zone:DNS:Edit на silentqa.com). До DNS работает всё, кроме публичного доступа/выпуска сертов (smoke локально через curl --resolve).
