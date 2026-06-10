# Relay healthcheck

Мониторинг работоспособности RU-relay инфраструктуры. Запускается на VPS #1 (`213.189.217.242`) каждые 5 минут через systemd timer. Алерты падают в Telegram-бот, который обрабатывает заявки `pbn-leads` (тот же `chat_id`, разные emoji-префиксы).

## Что проверяет

| Check | Что проверяет | Что считается ok |
|---|---|---|
| `systemd:gost-relay` | сервис активен | `is-active` |
| `systemd:nginx` | сервис активен | `is-active` |
| `proxy:reachable` | residential SOCKS5 отвечает и возвращает не наш IP | `curl --socks5h ipify` → IPv4, ≠ `213.189.217.242` |
| `http:rogov.automate-it.fun` | brokers relay endpoint | HTTP 200/301/302/401/403 |
| `http:rogov-estate.ru` | hybrid-сайт (статика) | HTTP 200 |
| `http:rogov-estate.ru/api` | API через relay → Hetzner backend | HTTP 200/401/404 |
| `http:muza-moscow.com` | PBN | HTTP 200 |
| `http:dom-seregina5.com` | PBN | HTTP 200 |
| `http:pogodinskaya-24.com` | PBN | HTTP 200 |
| `vps2:pbn-leads` | FastAPI на VPS #2 живой | HEAD `/api/lead` → 405/400/403 (не таймаут / не 5xx) |
| `cert:<domain>` | LE cert не истёк | warning ≤14 дней, alert ≤3 дней |
| `proxy_paid` | резидентский SOCKS5 оплачен | `PROXY_EXPIRES_AT` ≥сегодня + warning при ≤14, ≤7, ≤3, ≤1 дней |

## Файлы

| Path | Что |
|---|---|
| `/usr/local/bin/relay-healthcheck.sh` | главный bash-скрипт |
| `/etc/relay-healthcheck/env` | mode 600: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `PROXY_EXPIRES_AT`, `PROXY_PROVIDER` |
| `/etc/gost/proxy.env` | reused — содержит `PROXY_USER`/`PROXY_PASS`/`PROXY_HOST`/`PROXY_PORT` |
| `/etc/systemd/system/relay-healthcheck.service` | `Type=oneshot`, ExecStart=скрипт |
| `/etc/systemd/system/relay-healthcheck.timer` | `OnBootSec=2min`, `OnUnitActiveSec=5min` |
| `/var/lib/relay-healthcheck/state.json` | `{check_name: "ok"\|"fail"}` + `warned:<key>` для дневных warning'ов |
| `/var/log/relay-healthcheck.log` | append-лог запусков |

## Поведение алертов

- **ok → fail**: 🚨 алерт в Telegram, state записывает `fail`. Только один раз — пока не восстановится.
- **fail → ok**: ✅ recovery в Telegram, state записывает `ok`.
- **fail → fail**: тишина в Telegram (anti-spam), запись «still failing» в лог.
- **Cert/proxy expiry warnings** — отдельная семантика: фиксируется `warned:<key>=<UTC-date>` в state, повторно отправляется один раз в сутки пока актуально. Не блокирует ok→fail логику основных проверок.

Telegram-формат:
```
🚨 <b>Relay alert</b>
Check: <code>http:rogov-estate.ru/api</code>
Status: HTTP 502 (expected 200,401,404)
Time: 2026-05-08 14:02 UTC
```

## Operations

### Обновить дату оплаты прокси

Когда продлеваешь подписку на residential SOCKS5:

```bash
ssh root@213.189.217.242
sed -i 's/^PROXY_EXPIRES_AT=.*/PROXY_EXPIRES_AT=YYYY-MM-DD/' /etc/relay-healthcheck/env
# опционально удалить предыдущий warned-state чтобы новые warning'и были «свежими»
jq 'del(.["warned:proxy_paid"])' /var/lib/relay-healthcheck/state.json | sponge /var/lib/relay-healthcheck/state.json
```

Скрипт прочитает новое значение со следующего запуска (через ≤5 мин).

### Добавить новый сайт в проверки

Отредактировать `/usr/local/bin/relay-healthcheck.sh`:
1. Добавить строчку `check_http "<label>" "<url>" "<expected_codes>"` в секцию public-сайтов
2. Добавить домен в цикл `for d in ...; do check_cert_expiry "$d"; done`

После reload не нужен — `Type=oneshot` подхватит новую версию при следующем запуске.

### Намеренно подавить алерт (на время работ)

Сейчас явного suppression нет. Варианты:
- `systemctl stop relay-healthcheck.timer` на время работ, затем `start`
- В state.json вручную пометить check как `fail` — больше алертов не будет до явного recovery

### Проверить состояние

```bash
ssh root@213.189.217.242 'cat /var/lib/relay-healthcheck/state.json | jq'
ssh root@213.189.217.242 'tail -50 /var/log/relay-healthcheck.log'
ssh root@213.189.217.242 'systemctl list-timers relay-healthcheck.timer'
```

`state.json` пуст или близок к пустому = всё ok (сохраняются только `fail` и `warned:*` ключи).

### Запустить вне расписания

```bash
ssh root@213.189.217.242 'systemctl start relay-healthcheck.service'
```

## Ограничения

- **Не проверяет** валидность контента (например, что `index.html` содержит ожидаемый текст). Только HTTP-код. Достаточно для «ничего не упало», недостаточно для «сайт правильно собран».
- **Не проверяет** throughput SOCKS5 — только что отвечает. Для медленного proxy alert не сработает; броне-проверка throughput была бы дороже (нужен фиксированный test-payload и порог).
- **Telegram алерты приходят** в тот же чат что и лиды PBN — может смешиваться. Если станет много — разделить на два чата (отдельный `ALERT_CHAT_ID` в env).
- **Запускается только на VPS #1** — если сам VPS #1 упадёт, алертов не будет. Это известное ограничение; для защиты от VPS-down нужен внешний blackbox-monitor (UptimeRobot, etc.) — см. roadmap.
