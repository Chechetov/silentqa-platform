# RU Relay через residential SOCKS5

**Назначение.** Сделать сайты доступными RU-пользователям без VPN, когда транзит-провайдеры режут путь к зарубежному дата-центру (Hetzner шейпится по destination-IP). Используется для всего что упирается в это ограничение.

## Кто использует и как (production)

| Домен | VPS | Паттерн | Что лежит | Чем хостится backend |
|---|---|---|---|---|
| `rogov.automate-it.fun` | VPS #1 | full relay | nginx → gost → SOCKS5 → Hetzner | Hetzner uvicorn :8002 |
| `rogov-estate.ru` (+ www) | VPS #1 | **hybrid** | static → локально, `/api,/staff,/partners` → relay | static на VPS #1, API через relay → Hetzner uvicorn :8000 |
| `muza-moscow.com` (+ www) | VPS #2 | **standalone-RU** | nginx + статика | локальный pbn-leads :8770 |
| `dom-seregina5.com` (+ www) | VPS #2 | standalone-RU | nginx + статика | локальный pbn-leads :8770 |
| `pogodinskaya-24.com` (+ www) | VPS #2 | standalone-RU | nginx + статика | локальный pbn-leads :8770 |

Три паттерна — **выбираем по тому, насколько backend завязан на Hetzner**:
- **Full relay** — backend нельзя перенести (общая БД с другим сервисом, тяжёлая логика); статика тоже идёт через relay для простоты.
- **Hybrid** — статику можно отдать локально с RU IP (быстрее для аудитории), но часть API завязана на Hetzner-сервис.
- **Standalone-RU** — приложение целиком переносится на RU VPS. Никакой relay-зависимости. Самый дешёвый и быстрый паттерн.

## Топология

### Full relay (rogov.automate-it.fun)

```
RU User ─HTTPS─▶ rogov.automate-it.fun (DNS A → 213.189.217.242)
                       │
              VPS #1 nginx :443 (TLS termination, LE cert)
                       │ proxy_pass https://127.0.0.1:18443
                       ▼
              gost-relay.service (TCP forwarder)
                       │ chain через SOCKS5 residential RU IP
                       ▼
              Hetzner Caddy :443 (SNI=rogov.automate-it.fun) → uvicorn :8002
```

### Hybrid (rogov-estate.ru)

```
RU User ─HTTPS─▶ rogov-estate.ru (DNS A → 213.189.217.242)
                       │
              VPS #1 nginx :443
                       │
                       ├─ /api/, /staff/, /partners/  → gost → SOCKS5 → Hetzner :8000
                       │
                       └─ /  → /var/www/rogov-estate.ru/ (Astro dist, локально)
```

### Standalone-RU (3 PBN-сайта)

```
RU User ─HTTPS─▶ <pbn-domain> (DNS A → 5.181.255.154)
                       │
              VPS #2 nginx :443 (TLS termination, LE cert)
                       │
                       ├─ /api/lead → 127.0.0.1:8770 (pbn-leads FastAPI, локально)
                       │
                       └─ /  → /var/www/<pbn-domain>/ (статика, локально)
```

## Почему такая архитектура

**Проблема:** транзит NetAngels↔Hetzner режет sustained outbound к датацентровым destination-IP до 1-10 KB/s после ~130-200 KB. Это не сигнатурное DPI — никакая обфускация (Reality, mKCP, AmneziaWG, Hysteria2) не лечит. Подтверждено 2026-05-06.

**Cloudflare CDN-IP** не шейпятся, но конкретные edge-подсети сами по себе нестабильно работают из РФ (особенно `188.114.x`). У PBN-сайтов через CF-proxy реально не открывались — это и спровоцировало миграцию на standalone-RU.

**Residential SOCKS5** обходит shaper потому что для backend трафик выглядит как «домашний пользователь Hetzner» — этот паттерн ISP не режут. $5/мес, 50 Mbps, неогр. трафик. Один SOCKS5-эндпоинт обслуживает все relay-нужды (он outbound-shared).

## Файлы инфраструктуры

| Где | Что | Назначение |
|---|---|---|
| **VPS #1** `213.189.217.242` | NetAngels, 2 vCPU/2 GB | hybrid + full relay |
| `/etc/nginx/conf.d/relay.conf` | nginx server-blocks | TLS для rogov.automate-it.fun |
| `/etc/nginx/conf.d/rogov-estate.conf` | nginx server-block | TLS + hybrid routing rogov-estate.ru |
| `/etc/letsencrypt/live/<domain>/` | LE certs | автообновление через certbot.timer |
| `/etc/gost/proxy.env` | SOCKS5 creds | mode 600 |
| `/etc/systemd/system/gost-relay.service` | gost forwarder | один шлюз на Hetzner :443, переиспользуется всеми relay-доменами |
| `/usr/local/bin/gost` | go binary v3.x | |
| `/var/www/rogov-estate.ru/` | Astro dist | статика для hybrid |
| `/usr/local/bin/relay-healthcheck.sh` + systemd timer | мониторинг | см. [relay-healthcheck.md](relay-healthcheck.md) |
| **VPS #2** `5.181.255.154` | NetAngels, 4 vCPU/4 GB | standalone-RU для PBN |
| `/etc/nginx/conf.d/pbn-{muza,seregina,pogodinskaya}.conf` | server-blocks PBN | |
| `/var/www/<pbn-domain>/` | статика | синкается из Hetzner через `scripts/deploy-to-vps.sh` |
| `/opt/pbn-leads/` | FastAPI + SQLite | лид-форма для PBN, локальный экземпляр |
| `/etc/systemd/system/pbn-leads.service` | uvicorn unit | |
| **Hetzner** `/etc/caddy/Caddyfile` | legacy блоки rogov-estate.ru / PBN | оставлены для rollback, DNS на них не указывает |
| **Hetzner** `/home/dev/projects/PBN/` | source-of-truth статики PBN | деплой → VPS #2 через `scripts/deploy-to-vps.sh` |
| **Hetzner** `/home/dev/projects/rogov/rogov-estate-astro/` | source Astro | деплой → VPS #1 через `bin/deploy.sh` |

## Deploy

Все relay-сайты обновляются скриптами в исходных репозиториях (на Hetzner):

| Что обновить | Команда |
|---|---|
| `rogov-estate.ru` (Astro) | `cd /home/dev/projects/rogov/rogov-estate-astro && bin/deploy.sh` |
| Один PBN | `cd /home/dev/projects/PBN && scripts/deploy-to-vps.sh muza` |
| Все 3 PBN | `cd /home/dev/projects/PBN && scripts/deploy-to-vps.sh --all-live` |
| `rogov.automate-it.fun` | (только серверная часть, отдельный pipeline для `realestate` repo) |

Скрипты: `npm run build` (или `python3 scripts/generate-all.py` через `--regenerate`) → `rsync` → `chown www-data` → smoke-curl.

## Мониторинг

Реализован 2026-05-08 — см. отдельный runbook **[relay-healthcheck.md](relay-healthcheck.md)**:
- systemd timer на VPS #1, прогон каждые 5 минут
- проверки: gost-service, nginx-service, residential SOCKS5, 5 публичных endpoint-ов, cert expiry, оплата прокси
- алерты в тот же Telegram-бот, в который падают лиды PBN (`pbn-leads`)

## Runbook: добавить новый сайт

### Сценарий A: full relay (backend на Hetzner, нельзя перенести)

1. Тест из РФ — реально ли нужен relay (`curl -w 'time=%{time_total}s'`).
2. DNS A-запись для домена → `213.189.217.242`.
3. На VPS #1: `certbot --nginx -d <домен>` (HTTP-01 challenge сработает после propagation).
4. nginx server-block по образцу `/etc/nginx/conf.d/relay.conf` — `proxy_pass https://127.0.0.1:18443`, `proxy_ssl_name <домен>`, `Host: <домен>`.
5. Существующий `gost-relay.service` уже форвардит на Hetzner `:443` — новый gost-instance не нужен (Caddy маршрутизирует по SNI).
6. Hetzner Caddy: блок для домена должен существовать, `proxy_pass` на нужный backend.
7. Reload nginx, smoke `curl -sI https://<домен>/`.
8. Добавить домен в `/usr/local/bin/relay-healthcheck.sh` (`check_http` секция) и в state-warned-list для cert.

### Сценарий B: hybrid (статика на RU, часть API через relay)

Как Сценарий A + дополнительно:
- Статика рснкается на VPS #1 в `/var/www/<домен>/` (создать deploy-скрипт типа `bin/deploy.sh` в исходном repo).
- nginx server-block с двумя location'ами: `~ ^/(api|staff|partners)/` → `proxy_pass https://127.0.0.1:18443`, `/` → `try_files`.
- Отдельный strict-404 location для assets (`_astro|favicon|...`) с `try_files $uri =404` — иначе при отсутствии файла nginx fallback'ит на `index.html` с Content-Type: text/html, что ломает CSS/JS.

### Сценарий C: standalone-RU (приложение полностью переносим)

Если приложение можно перенести и оно не нуждается в Hetzner backend:
1. На целевом VPS (`5.181.255.154` или новый): nginx + certbot + ufw (22/80/443).
2. Рснкаем статику в `/var/www/<домен>/`.
3. Если есть FastAPI/прочее — переносим вместе с venv, .env, systemd unit.
4. nginx server-block: `listen 80; certbot --nginx -d <домен> -d www.<домен>` авто-добавит HTTPS.
5. DNS A-запись на VPS IP, **без CF-proxy** (серое облако).
6. Smoke + добавить в `relay-healthcheck.sh`.

Шаблон-скрипт деплоя — `scripts/deploy-to-vps.sh` в PBN repo, легко адаптируется под другой проект.

## Что НЕЛЬЗЯ забыть

1. **Target host в gost = публичный IP Hetzner (`46.224.72.186`), не доменное имя**. Иначе residential proxy резолвит домен → видит NetAngels-IP → бесконечный loop.

2. **`proxy_ssl_name` в nginx = реальный SNI** для inner-TLS handshake. Hetzner Caddy маршрутизирует по SNI, иначе ответит дефолтным сайтом или 404.

3. **Hybrid: strict-404 для assets** — `try_files $uri =404` на `_astro|logos|*.css|*.js` обязателен, иначе при rebuild без последующего rsync nginx отдаёт index.html на запрос CSS, и браузер видит «мобильную версию» (default browser styles, без CSS).

4. **Caddy на Hetzner всё ещё пытается renew'ать cert** для зарелеенных доменов (`rogov.automate-it.fun`, `rogov-estate.ru`) через HTTP-01/TLS-ALPN-01, но DNS теперь на NetAngels — challenges провалятся. Текущие certs валидны до ~2026-08-04. После — inner-TLS handshake (VPS #1 nginx → Hetzner Caddy) сломается. Решения: (a) `proxy_ssl_verify off` в nginx VPS #1 (прагматично), (b) DNS-01 challenge для Caddy через Hetzner DNS API (правильно), (c) plain-HTTP forwarding на отдельный порт Hetzner. **Дедлайн: ~июль 2026.**

5. **Residential proxy expires/quota** — если SOCKS5 перестанет отвечать, relay сломается с 502. Мониторится через `relay-healthcheck.sh` (см. `PROXY_EXPIRES_AT` в `/etc/relay-healthcheck/env` — обновлять при оплате).

6. **Rsync после rebuild** — статика на VPS #1/#2 не апдейтится автоматически. Используй deploy-скрипты, не ручной rsync.

## Когда переходить на Cloudflare Tunnel

Если столкнёмся с одним из:
- Residential proxy провайдер ушёл с рынка / аккаунт забанили
- Bandwidth превысил $5-план
- NetAngels-VPS стал нестабильным
- Roskomnadzor начал блокировать конкретно IP NetAngels

→ переносим `automate-it.fun` (и связанные) на Cloudflare DNS, ставим `cloudflared` на Hetzner, настраиваем tunnel'ы. CF не блокируется массово в РФ; CF backbone обходит RU транзит. Free tier на 100 MB body — с запасом для нашего use-case.

## Кост

| Компонент | Стоимость |
|---|---|
| NetAngels VPS #1 (`213.189.217.242`, 2 vCPU/2 GB) | ~500₽/мес ($5) |
| NetAngels VPS #2 (`5.181.255.154`, 4 vCPU/4 GB) | ~500₽/мес ($5) |
| Residential SOCKS5 (50 Mbps unlim) | $5/мес |
| Let's Encrypt certs | бесплатно |
| **Итого** | **~$15/мес** на 5 доменов и любое количество будущих |

## История попыток

Попытки решить проблему через client-side протоколы 2026-05-06 (~6 часов работы):

| Попытка | Результат |
|---|---|
| Прямой nginx reverse-proxy на Hetzner | ❌ 192 KB hard cap |
| `nf_conntrack_tcp_be_liberal=1` | ❌ не помогает (это не conntrack issue) |
| iptables MSS clamp / TSO/GSO off | ❌ не помогает |
| Xray VLESS+Reality (TCP) | ❌ handshake works, body upload throttles |
| Xray VLESS+mKCP (UDP) | ❌ same |
| AmneziaWG (UDP+обфускация) | ❌ handshake works, после ~130 KB пакеты дропаются |
| Hysteria2 + port hopping | ❌ same pattern |
| vk-turn-proxy (TURN через VK servers) | ⚠️ архитектурно работает, но VK captcha workflow не подходит для server-to-server без RU-residential captcha solver |
| 2-hop через AWS Stockholm EC2 (clean middleman) | ❌ AWS direct EC2 IP тоже зашейплен |
| **Residential SOCKS5 chain** | ✅ **750 KB/s sustained, нулевой шейпинг** |

Подробности в `docs/superpowers/specs/2026-05-06-russia-relay-vps-design.md`.
