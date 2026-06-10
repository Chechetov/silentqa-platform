# Российский relay для десктоп-приложения

**Дата:** 2026-05-06
**Статус:** ✅ реализовано (в production), архитектура отличается от первоначального плана
**Финальный runbook:** `docs/architecture/ru-relay.md`

## Контекст

Брокеры из РФ не могли надёжно загружать записи Zoom/телемост в backend на Hetzner Falkenstein без VPN. Десктоп-приложение клиентов посылает аудио-чанки на `rogov.automate-it.fun`, прямой путь до Hetzner режется на уровне транзит-провайдеров.

## Финальная архитектура

```
Брокер РФ ──HTTPS──▶ rogov.automate-it.fun (DNS на NetAngels VPS 213.189.217.242)
                       │
                       ▼ nginx :443 — TLS termination, Let's Encrypt
                  proxy_pass https://127.0.0.1:18443
                       │
                       ▼ gost-relay.service (TCP forwarder)
                  SOCKS5-chain через user406545:di06p9@158.46.250.71:11331 (residential RU IP)
                       │
                       ▼ HTTPS (SNI=rogov.automate-it.fun) → 46.224.72.186:443 (Hetzner Caddy)
                  Caddy → uvicorn :8002
```

**Throughput замеренный в проде:** 750-840 KB/s sustained, latency +50-100ms vs прямой путь.

## Почему архитектура отличается от плана

**Изначальный план (см. историю в этом документе ниже):** простой nginx reverse-proxy на NetAngels VPS, прямо проксирующий на Hetzner. **Не сработал** — транзит NetAngels↔Hetzner режет sustained TLS-аплоады на ~130-200 KB.

**Перебор протоколов** (Reality, mKCP, AmneziaWG, Hysteria2, vk-turn-proxy) **не помог** — shaping не сигнатурный, а destination-IP-based. Никакая обфускация не лечит.

**Решение:** residential SOCKS5-прокси, $5/мес. Делает трафик «домашний пользователь → Hetzner» — этот паттерн ISP не режут. Работает на 750+ KB/s стабильно.

## Архитектурные решения зафиксированы в

`docs/architecture/ru-relay.md` — runbook для добавления новых сайтов в relay (PBN, и т.д.). Этот spec остаётся как **исторический документ** проблемы и процесса принятия решений.

## Известные хвосты

1. **Caddy cert renewal для `rogov.automate-it.fun`** перестанет работать через ~3 месяца (после 2026-08-04). Нужно решить: DNS-01 challenge для Caddy, либо `proxy_ssl_verify off` в NetAngels nginx, либо plain-HTTP forwarding на отдельный порт Hetzner. См. `docs/architecture/ru-relay.md#что-нельзя-забыть`.

2. **Healthcheck/мониторинг** не настроен. Запланирован: cron на NetAngels раз в 5 мин, алерт в Telegram-bot.

3. **AWS SSH-ключ ротировать** — приватный ключ светился через tmpfiles.org во время setup'а. EC2 console → key pair regenerate.

4. **Spec-doc этот** — обновить если/когда добавим CF Tunnel или мигрируем на другой подход.

## История проблемы и попыток (для ретроспективы)

### Изначальная гипотеза (~10:00 UTC)

Транзит NetAngels↔Hetzner блокирует HTTPS-аплоады по DPI-сигнатуре. План: nginx reverse-proxy с большим body buffer на NetAngels.

**Реальность:** прямой nginx упёрся в **жёсткий 192 KB cap** на любые multipart-аплоады к Hetzner. Нагрузка не идёт дальше первых ~196 608 байт, никакой ответ не приходит.

### Попытка фикса через kernel/nginx-tuning (~11:00 UTC)

- `nf_conntrack_tcp_be_liberal=1` — не помог
- iptables MSS clamp on VPS — не помог
- TSO/GSO disable — не помог
- HTTP/1.1 vs HTTP/2 — не помог
- request_buffering on/off — не помог

**Вывод:** проблема НЕ в conntrack/MTU/TCP-tuning. Это shaping выше уровня kernel.

### Перебор обфусцирующих протоколов (~13:00-17:00 UTC)

Гипотеза: shaping срабатывает по DPI-сигнатуре HTTPS-загрузки. Нужно скрыть протокол.

**Установлены и протестированы:**

| Протокол | Что обещает | Что получили |
|---|---|---|
| Xray VLESS+Reality (TCP) | TLS-imitation, копия handshake популярного сайта | Handshake works, body upload throttles после ~130-200 KB |
| Xray VLESS+mKCP (UDP) | UDP-based, скрывается под srtp/wireguard headers | Same pattern |
| AmneziaWG | WireGuard с junk-обфускацией под российский ТСПУ | Handshake works, после первых пакетов 100% packet loss |
| Hysteria2 + port hopping | QUIC, рандомизация портов 30000-30100 для evasion shaping tuple | Same pattern: 5MB upload только 130 KB прошло |
| vk-turn-proxy через VK TURN | Туннель через whitelisted VK звонки IPs | Архитектурно работает, но VK captcha workflow требует RU-IP solver — не для админа из Португалии |

**Все упёрлись в один паттерн:** мелкие запросы летят, sustained body upload >130-200 KB throttles до 1-10 KB/s.

### Финальный диагноз (~17:30 UTC)

Тестирование 5MB upload на разные destination'ы с того же VPS:

| Destination | Throughput |
|---|---|
| httpbin.org / Cloudflare 104.x | **1.3 MB/s** ✅ |
| api.openai.com | 900 KB/s ✅ (но 403 от OpenAI с RU IP) |
| yandex.ru, mail.ru | 1.3 MB/s ✅ |
| **AWS Stockholm EC2 13.60.x (direct VM)** | **4 KB/s** ❌ |
| **AssemblyAI us-west-2 34.217.x** | **3 KB/s** ❌ |
| **Hetzner FRA 46.224.x** | **10 KB/s** ❌ |

**Окончательный вывод:** shaping NetAngels (или upstream) — **destination-IP-based**, не сигнатурный. Прицельно режет известные cloud-VM подсети. Cloudflare CDN-IP'ы и популярные RU-сайты — clean.

### Решение (~19:00 UTC)

Купили residential RU SOCKS5-прокси за $5/мес ($5/50 Mbps unlim). Прокси-IP это домашний/мобильный пул, который ISP не считают «server traffic» и не режут.

Архитектура: nginx → gost (TCP forwarder с SOCKS5 chaining) → residential proxy → Hetzner.

**Тест прошёл с первой попытки**: 5 MB → 750 KB/s, 50 MB → 842 KB/s, ответ от backend в реальном времени. В дальнейшем DNS `rogov.automate-it.fun` переключён с Hetzner на NetAngels — брокеры в десктоп-приложении ничего не меняли.

## Ссылки

- **Runbook:** `docs/architecture/ru-relay.md` — как добавить новый сайт в relay
- **Memory:** `~/.claude/projects/-root-projects-realestate/memory/project_relay_residential.md` — состояние production
- **Memory:** `~/.claude/projects/-root-projects-realestate/memory/project_netangels_dpi.md` — характеристика shaping
