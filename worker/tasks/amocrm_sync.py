"""
Синхронизация результатов обработки звонков с AmoCRM.

Функции:
- Поиск сделки по номеру телефона
- Создание обогащённого примечания (summary + client_info + оценка + ссылка)
- Обновление существующего примечания при повторной обработке
- Автообновление OAuth2 токена через refresh_token
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta

import psycopg2
import requests

from tenancy.context import get_tenant_slug
from tenancy.db import shared_connect

logger = logging.getLogger(__name__)

AMOCRM_BASE_URL = os.getenv("AMOCRM_BASE_URL", "https://rogovestate.amocrm.ru")
DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL", "https://rogov.automate-it.fun")

_DASHBOARD_URL_CACHE: dict[str, str] = {}


def _dashboard_base_url() -> str:
    """База ссылок на дашборд для текущего тенанта (спека 5.5).

    Приоритет: shared.tenants.dashboard_base_url → https://{slug}.{BASE_DOMAIN}
    → env DASHBOARD_BASE_URL (dev-фоллбек без тенант-контекста).
    """
    slug = get_tenant_slug()
    if slug is None:
        return DASHBOARD_BASE_URL
    if slug in _DASHBOARD_URL_CACHE:
        return _DASHBOARD_URL_CACHE[slug]
    url = None
    try:
        conn = shared_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT dashboard_base_url FROM shared.tenants WHERE slug = %s",
                    (slug,),
                )
                row = cur.fetchone()
                url = row[0] if row and row[0] else None
        finally:
            conn.close()
    except Exception:
        logger.exception("dashboard_base_url lookup failed; using fallback")
    if not url:
        url = f"https://{slug}.{os.getenv('BASE_DOMAIN', 'silentqa.com')}"
    _DASHBOARD_URL_CACHE[slug] = url
    return url

# Token is owned by rogov-partner-portal and stored in portal.amocrm_tokens.
# This worker reads-only to avoid racing the portal's refresh_token rotation,
# which previously caused "Token has been revoked" 401s across all consumers.
_TOKEN_CACHE_TTL_SEC = 60
_token_lock = threading.Lock()
_cached_access_token: str = ""
_cached_at: float = 0.0
_env_fallback_token: str = os.getenv("AMOCRM_ACCESS_TOKEN", "")

# Статусы-замыкатели в AmoCRM
WON_STATUS = 142
LOST_STATUS = 143


def _read_token_from_portal_db() -> str | None:
    """Read the current access_token from the portal's source of truth."""
    db_url = os.getenv("DATABASE_URL_SYNC", "") or os.getenv("DATABASE_URL", "")
    if not db_url:
        return None
    db_url = db_url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT access_token FROM portal.amocrm_tokens WHERE id = 1")
                row = cur.fetchone()
        finally:
            conn.close()
        return row[0] if row and row[0] else None
    except Exception:
        logger.exception("Failed to read AmoCRM access_token from portal.amocrm_tokens")
        return None


def _get_access_token() -> str:
    """Return a fresh access_token read from the portal's token store."""
    global _cached_access_token, _cached_at

    now = time.monotonic()
    if _cached_access_token and now - _cached_at < _TOKEN_CACHE_TTL_SEC:
        return _cached_access_token

    with _token_lock:
        if _cached_access_token and time.monotonic() - _cached_at < _TOKEN_CACHE_TTL_SEC:
            return _cached_access_token
        token = _read_token_from_portal_db()
        if token:
            _cached_access_token = token
            _cached_at = time.monotonic()
            return token
        return _env_fallback_token


def _invalidate_token_cache() -> None:
    """Drop the cached token so the next call re-reads from the portal DB."""
    global _cached_access_token, _cached_at
    with _token_lock:
        _cached_access_token = ""
        _cached_at = 0.0


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_access_token()}",
        "Content-Type": "application/json",
    }


def _amo_request(method: str, url: str, **kwargs) -> requests.Response:
    """Make an authenticated request to AmoCRM; on 401 refresh the token once and retry.

    Handles the race where the partner portal rotated the token but this
    worker's 60-sec cache still holds the old value.
    """
    user_headers = kwargs.pop("headers", None) or {}
    merged = {**_headers(), **user_headers}
    resp = requests.request(method, url, headers=merged, **kwargs)
    if resp.status_code == 401:
        logger.info("AmoCRM 401 → invalidating cached token and retrying once")
        _invalidate_token_cache()
        merged = {**_headers(), **user_headers}
        resp = requests.request(method, url, headers=merged, **kwargs)
    return resp


def _normalize_phone(phone: str) -> str:
    """Нормализация номера к формату для поиска в AmoCRM."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    return digits


MAX_EVENT_PAGES = 50  # Safety limit: 50 pages * 100 = 5000 events max


def get_recent_call_events(since_timestamp: int) -> list[dict]:
    """
    Fetch recent call events from AmoCRM since given unix timestamp.
    Returns list of dicts with: note_id, entity_id, entity_type, event_type.
    """
    if not _get_access_token():
        return []

    events = []
    page = 1
    while True:
        try:
            resp = _amo_request(
                "GET",
                f"{AMOCRM_BASE_URL}/api/v4/events",
                params={
                    "filter[type][]": ["incoming_call", "outgoing_call"],
                    "filter[created_at][from]": since_timestamp,
                    "limit": 100,
                    "page": page,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(f"AmoCRM events fetch failed: {resp.status_code}")
                break

            data = resp.json()
            items = data.get("_embedded", {}).get("events", [])
            if not items:
                break

            for event in items:
                entity_id = event.get("entity_id")
                entity_type = event.get("entity_type")
                event_type = event.get("type")
                value_after = event.get("value_after", [])
                note_id = None
                for v in value_after:
                    if "note" in v:
                        note_id = v["note"].get("id")
                        break

                if note_id and entity_id:
                    events.append({
                        "note_id": note_id,
                        "entity_id": entity_id,
                        "entity_type": entity_type or "leads",
                        "event_type": event_type,
                    })

            next_link = data.get("_links", {}).get("next")
            if not next_link:
                break
            page += 1
            if page > MAX_EVENT_PAGES:
                logger.warning(f"Reached max event pages ({MAX_EVENT_PAGES}), stopping pagination")
                break

        except Exception:
            logger.exception("Error fetching AmoCRM events")
            break

    logger.info(f"Fetched {len(events)} call events from AmoCRM since {since_timestamp}")
    return events


def get_note_details(entity_type: str, entity_id: int, note_id: int) -> dict | None:
    """Fetch full note details from AmoCRM. Returns note dict or None."""
    if not _get_access_token():
        return None

    entity_path = entity_type if entity_type.endswith("s") else f"{entity_type}s"

    try:
        resp = _amo_request(
            "GET",
            f"{AMOCRM_BASE_URL}/api/v4/{entity_path}/{entity_id}/notes/{note_id}",
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning(f"Failed to get note {note_id}: {resp.status_code}")
        return None
    except Exception:
        logger.exception(f"Error fetching note {note_id}")
        return None


def get_lead_with_contacts(lead_id: int) -> dict | None:
    """Fetch a lead with linked contacts. Returns dict with 'contact_ids' key or None."""
    if not _get_access_token():
        return None
    try:
        resp = _amo_request(
            "GET",
            f"{AMOCRM_BASE_URL}/api/v4/leads/{lead_id}",
            params={"with": "contacts"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"Failed to fetch lead {lead_id}: {resp.status_code}")
            return None
        data = resp.json()
        contact_ids = [c["id"] for c in data.get("_embedded", {}).get("contacts", [])]
        return {"lead_id": lead_id, "contact_ids": contact_ids, "raw": data}
    except Exception:
        logger.exception(f"Error fetching lead {lead_id}")
        return None


def _phone_from_contact(contact: dict) -> str:
    for field in (contact.get("custom_fields_values") or []):
        if field.get("field_code") == "PHONE":
            for v in field.get("values") or []:
                val = v.get("value")
                if val:
                    return val
    return ""


def search_leads(query: str, limit: int = 20) -> list[dict]:
    """Free-form AmoCRM lead search (matches name / phone / email).

    Returns up to `limit` results enriched with the linked contact's name and
    phone, in the shape the dashboard's lead-picker UI expects:
      {lead_id, lead_name, contact_name, phone, status_id, pipeline_id, price}
    """
    if not query or len(query.strip()) < 2:
        return []
    if not _get_access_token():
        return []

    try:
        resp = _amo_request(
            "GET",
            f"{AMOCRM_BASE_URL}/api/v4/leads",
            params={"query": query.strip(), "limit": limit, "with": "contacts"},
            timeout=10,
        )
    except Exception:
        logger.exception("AmoCRM lead search failed")
        return []
    if resp.status_code == 204:
        return []
    if resp.status_code != 200:
        logger.warning(f"AmoCRM lead search HTTP {resp.status_code}")
        return []

    leads = resp.json().get("_embedded", {}).get("leads", []) or []
    if not leads:
        return []

    # Bulk-fetch contact details for every linked contact in one round-trip per 50.
    contact_ids: list[int] = []
    for lead in leads:
        for c in (lead.get("_embedded", {}).get("contacts") or []):
            cid = c.get("id")
            if cid:
                contact_ids.append(cid)
    contact_by_id: dict[int, dict] = {}
    for i in range(0, len(set(contact_ids)), 50):
        batch = list(set(contact_ids))[i:i + 50]
        params = "&".join(f"filter[id][]={cid}" for cid in batch)
        try:
            cr = _amo_request("GET", f"{AMOCRM_BASE_URL}/api/v4/contacts?{params}", timeout=10)
            if cr.status_code == 200:
                for c in cr.json().get("_embedded", {}).get("contacts", []):
                    contact_by_id[c["id"]] = c
        except Exception:
            logger.exception("AmoCRM contact bulk-fetch failed")

    out: list[dict] = []
    for lead in leads:
        first_contact_name = ""
        first_phone = ""
        for c in (lead.get("_embedded", {}).get("contacts") or []):
            ci = contact_by_id.get(c.get("id"))
            if ci:
                first_contact_name = ci.get("name") or ""
                first_phone = _phone_from_contact(ci)
                if first_contact_name or first_phone:
                    break
        out.append({
            "lead_id": lead.get("id"),
            "lead_name": lead.get("name") or "",
            "contact_name": first_contact_name,
            "phone": first_phone,
            "status_id": lead.get("status_id"),
            "pipeline_id": lead.get("pipeline_id"),
            "price": lead.get("price"),
        })
    return out


def list_call_notes_on_entity(entity_type: str, entity_id: int) -> list[dict]:
    """
    List call_in/call_out notes on a given entity (lead or contact).
    Returns raw note dicts from AmoCRM (id, note_type, params, etc.). Empty list on 204/errors.
    """
    if not _get_access_token():
        return []
    entity_path = entity_type if entity_type.endswith("s") else f"{entity_type}s"
    try:
        resp = _amo_request(
            "GET",
            f"{AMOCRM_BASE_URL}/api/v4/{entity_path}/{entity_id}/notes",
            params={
                "limit": 100,
                "filter[note_type][]": ["call_in", "call_out"],
            },
            timeout=15,
        )
        if resp.status_code == 204:
            return []
        if resp.status_code != 200:
            logger.warning(f"Failed to list notes on {entity_type}/{entity_id}: {resp.status_code}")
            return []
        return resp.json().get("_embedded", {}).get("notes", [])
    except Exception:
        logger.exception(f"Error listing notes on {entity_type}/{entity_id}")
        return []


# Recording hosts (media.comagic.ru / UIS) are in RU. Hetzner->RU egress is
# intermittently blocked (~50% of connects time out), so downloads are routed
# through a SOCKS5 tunnel that egresses from the KZ relay, where RU is reachable.
# Empty string disables the proxy (direct only). See realestate-kz-socks.service.
RECORDING_PROXY = os.getenv("RECORDING_PROXY", "socks5h://127.0.0.1:1080")


def _stream_to_file(url: str, dest_path: str, proxies: dict | None) -> bool | None:
    """Download `url` to `dest_path`. Returns True on success, False on a
    definitive 404 (don't retry/fall back), or raises on a transport error."""
    resp = requests.get(url, stream=True, timeout=60, proxies=proxies)
    if resp.status_code >= 400:
        # We reached the origin and it returned an error (404 expired, 400
        # "no files to concat" for calls with no recording, etc.). Switching
        # egress routes can't change the server's answer, so this is definitive
        # — return False rather than raising, so we don't waste a 60s timeout
        # on the direct-connection fallback. Transport errors (timeout/refused)
        # still raise below and DO trigger the fallback.
        logger.warning(f"Recording unavailable (HTTP {resp.status_code}): {url}")
        return False

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

    file_size = os.path.getsize(dest_path)
    logger.info(f"Downloaded recording: {dest_path} ({file_size / 1024:.0f} KB)")
    return True


def download_recording(url: str, dest_path: str) -> bool:
    """Download a call recording. Routes through the KZ SOCKS5 egress proxy first
    (Hetzner->RU is intermittently blocked; KZ->RU is stable), then falls back to
    a direct connection if the proxy itself is unavailable. Returns True on
    success."""
    if not url:
        return False

    # (label, proxies) attempts, in order. Proxy first, direct as fallback.
    attempts: list[tuple[str, dict | None]] = []
    if RECORDING_PROXY:
        attempts.append(("KZ proxy", {"http": RECORDING_PROXY, "https": RECORDING_PROXY}))
    attempts.append(("direct", None))

    for label, proxies in attempts:
        try:
            result = _stream_to_file(url, dest_path, proxies)
            if result is False:
                return False  # definitive 404 — no point trying other routes
            return True
        except Exception as e:
            logger.warning(f"Recording download via {label} failed for {url}: {e}")

    logger.error(f"Failed to download recording from {url} (all routes exhausted)")
    return False


def find_lead_by_phone(phone: str) -> int | None:
    """
    Ищет контакт в AmoCRM по номеру телефона → возвращает lead_id самой свежей активной сделки.
    Возвращает None если ничего не нашли.
    """
    if not _get_access_token() or not phone:
        return None

    normalized = _normalize_phone(phone)
    if not normalized or len(normalized) < 7:
        return None

    # AmoCRM contact search is substring-based, but contact cards store RU
    # numbers with the '8' trunk prefix (89281304030) while call notes carry
    # the dialed +7 form. '79281304030' is NOT a substring of '89281304030'
    # (different leading digit), so searching by the normalized 7-form returns
    # 204. Search by the last 10 digits (the subscriber number), which is a
    # substring of every stored variant: 8XXXXXXXXXX, 7XXXXXXXXXX, +7XXXX...
    search_query = normalized[-10:] if len(normalized) >= 10 else normalized

    try:
        # Поиск контактов по телефону
        resp = _amo_request(
            "GET",
            f"{AMOCRM_BASE_URL}/api/v4/contacts",
            params={"query": search_query, "with": "leads"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"AmoCRM contact search failed: {resp.status_code}")
            return None

        contacts = resp.json().get("_embedded", {}).get("contacts", [])
        if not contacts:
            logger.info(f"No AmoCRM contact found for phone {normalized}")
            return None

        # Собираем все связанные сделки из всех найденных контактов
        lead_ids = []
        for contact in contacts:
            leads = contact.get("_embedded", {}).get("leads", [])
            for lead in leads:
                lead_ids.append(lead["id"])

        if not lead_ids:
            logger.info(f"Contact found but no linked leads for phone {normalized}")
            return None

        # Загружаем сделки и берём самую свежую активную (не закрытую)
        best_lead = None
        best_date = 0
        for i in range(0, len(lead_ids), 50):
            batch = lead_ids[i:i + 50]
            params = "&".join(f"filter[id][]={lid}" for lid in batch)
            resp = _amo_request(
                "GET",
                f"{AMOCRM_BASE_URL}/api/v4/leads?{params}",
                timeout=10,
            )
            if resp.status_code != 200:
                continue
            for lead in resp.json().get("_embedded", {}).get("leads", []):
                status = lead.get("status_id", 0)
                if status in (WON_STATUS, LOST_STATUS):
                    continue  # Пропускаем закрытые
                updated = lead.get("updated_at", 0)
                if updated > best_date:
                    best_date = updated
                    best_lead = lead["id"]

        if best_lead:
            logger.info(f"Found active lead {best_lead} for phone {normalized}")
        else:
            # Если нет активных, берём последнюю обновлённую
            best_lead = lead_ids[0] if lead_ids else None
            logger.info(f"No active leads, using most recent: {best_lead}")

        return best_lead

    except Exception:
        logger.exception(f"Error searching AmoCRM for phone {phone}")
        return None


# Cached AmoCRM user_id → name mapping
_users_cache: dict[int, str] = {}
_users_cache_loaded = False


def _load_users_cache():
    """Fetch AmoCRM users and cache id→name mapping."""
    global _users_cache, _users_cache_loaded
    if _users_cache_loaded:
        return
    try:
        resp = _amo_request(
            "GET",
            f"{AMOCRM_BASE_URL}/api/v4/users",
            timeout=10,
        )
        if resp.status_code == 200:
            for u in resp.json().get("_embedded", {}).get("users", []):
                _users_cache[u["id"]] = u.get("name", "")
            _users_cache_loaded = True
            logger.info(f"Loaded {len(_users_cache)} AmoCRM users")
    except Exception:
        logger.warning("Failed to load AmoCRM users cache")


def _resolve_broker_name(quality_report: dict, responsible_user_id: int = 0) -> str | None:
    """Resolve broker full name: first try AmoCRM user mapping, fallback to speaker_roles."""
    # Try matching by AmoCRM responsible_user_id
    if responsible_user_id:
        _load_users_cache()
        name = _users_cache.get(responsible_user_id)
        if name and "@" not in name:  # Skip email-style names
            return name

    # Fallback: name from GPT speaker_roles (only first name)
    speaker_roles = quality_report.get("speaker_roles", {})
    for sid, info in speaker_roles.items():
        if info.get("role") == "manager" and info.get("name"):
            return info["name"]
    return None


def format_enriched_note(quality_report: dict, session_id: str, responsible_user_id: int = 0) -> str:
    """
    Формирует текст обогащённого примечания для AmoCRM.
    Формат адаптируется под тип звонка (brush-off / продуктивный / короткий).
    """
    # Short-circuit: calls too short for meaningful evaluation
    if quality_report.get("skip_reason") == "too_short":
        secs = int(quality_report.get("audio_duration", 0))
        return (
            f"⚠️ Короткий звонок ({secs}с).\n\n"
            f"Оценка не проводилась — вероятно, автоответчик "
            f"или клиент не ответил."
        )

    # Short-circuit: broken client recording (mic stream died mid-call)
    if quality_report.get("skip_reason") == "broken_client_recording":
        secs = int(quality_report.get("audio_duration", 0))
        stats = quality_report.get("silence_stats") or {}
        return (
            f"⚠️ Битая запись ({secs}с).\n\n"
            f"После начала разговора микрофон перестал давать звук — "
            f"типично при переключении аудио-устройства (наушники/Bluetooth) "
            f"во время записи.\n"
            f"Тишина в {stats.get('silent_windows','?')}/"
            f"{stats.get('total_windows','?')} пробах хвоста записи. "
            f"Оценка не проводилась, аудио по факту пустое."
        )

    score = quality_report.get("overall_score")
    classification = quality_report.get("call_classification", {})
    call_type = classification.get("type", "")
    brief_summary = quality_report.get("brief_summary", "")
    client_info = quality_report.get("client_info", {})
    client_info_summary = quality_report.get("client_info_summary", "")
    summary = quality_report.get("summary", "")
    checklist = quality_report.get("protocol_checklist", [])
    objections = quality_report.get("objections", [])
    outcome = quality_report.get("conversation_outcome", {})

    # Заголовок с оценкой и классификацией
    class_labels = {
        "brushoff_short": "Brush-off (короткий)",
        "brushoff_with_attempt": "Brush-off (с попыткой)",
        "partial": "Частичный",
        "productive": "Продуктивный",
        "meeting_scheduled": "Встреча назначена",
    }
    class_label = class_labels.get(call_type, call_type)

    broker_name = _resolve_broker_name(quality_report, responsible_user_id)

    parts = []

    # Строка 1: оценка + брокер + тип
    score_str = f"{score}/10" if score is not None else "—"
    broker_str = f" | {broker_name}" if broker_name else ""
    parts.append(f"Оценка: {score_str}{broker_str} | Исходящий | {class_label}")
    parts.append("")

    # Краткое summary
    if brief_summary:
        parts.append(brief_summary)
    elif summary:
        parts.append(summary)
    parts.append("")

    # Для brush-off: показываем попытки брокера
    if call_type in ("brushoff_short", "brushoff_with_attempt"):
        if checklist:
            parts.append("Попытки брокера:")
            for group in checklist:
                for item in group.get("items", []):
                    status = item.get("status", "")
                    icon = {"completed": "✓", "attempted": "~", "not_reached": "✗"}.get(status, "—")
                    if status in ("completed", "attempted"):
                        suffix = f" ({item.get('comment', '')})" if item.get("comment") and status == "attempted" else ""
                        parts.append(f"{icon} {item['name']}{suffix}")
            parts.append("")

        # Возражения
        if objections:
            parts.append("Возражения:")
            for obj in objections:
                resolved = "снято" if obj.get("resolved") else "не снято"
                parts.append(f"• \"{obj.get('text', '')}\" — {resolved}")
            parts.append("")

    else:
        # Для продуктивных звонков: информация от клиента
        info_items = []
        field_labels = {
            "budget": "Бюджет", "locations": "Район", "apartment_format": "Формат",
            "timeline": "Сроки", "payment_form": "Оплата", "purchase_goal": "Цель",
            "important_factors": "Важно", "what_viewed": "Смотрели",
        }
        for key, label in field_labels.items():
            val = client_info.get(key)
            if val:
                info_items.append(f"• {label}: {val}")

        if info_items:
            parts.append("Получено от клиента:")
            parts.extend(info_items)
            parts.append("")
        elif client_info_summary:
            parts.append(f"От клиента: {client_info_summary}")
            parts.append("")

        # Встреча — ключевая метрика для исходящих
        meeting_scheduled = (outcome.get("result") == "appointment" or call_type == "meeting_scheduled")
        meeting_attempts = 0
        meeting_arguments = []
        for group in checklist:
            if group.get("id") == "meeting":
                for item in group.get("items", []):
                    if item["id"] in ("meeting_proposed", "meeting_second_attempt") and item.get("status") in ("completed", "attempted"):
                        meeting_attempts += 1
                    if item.get("status") == "completed" and item.get("comment"):
                        meeting_arguments.append(item["comment"])
        # Also check general_checks for attempt count
        for check in quality_report.get("general_checks", []):
            if check.get("id") == "meeting_proposed_2_times":
                meeting_attempts = max(meeting_attempts, check.get("count") or 0)

        if meeting_scheduled:
            parts.append("Встреча: назначена ✓")
        else:
            parts.append(f"Встреча: не назначена")
        parts.append(f"Попыток назначить: {meeting_attempts}")
        if meeting_arguments:
            parts.append("Аргументация:")
            for arg in meeting_arguments:
                parts.append(f"  • {arg}")
        parts.append("")

        # Прогресс по чек-листу
        if checklist:
            total = 0
            completed = 0
            for group in checklist:
                for item in group.get("items", []):
                    if item.get("status") != "not_applicable":
                        total += 1
                    if item.get("status") == "completed":
                        completed += 1
            if total > 0:
                pct = round(completed / total * 100)
                parts.append(f"Прогресс по скрипту: {pct}% ({completed}/{total})")

        # Результат
        result = outcome.get("result", "")
        result_desc = outcome.get("description", "")
        if not meeting_scheduled and result_desc:
            parts.append(f"Итог: {result_desc}")

        parts.append("")

    # Возражения (для всех типов звонков)
    if objections:
        parts.append("Возражения клиента:")
        for obj in objections:
            resolved = "✓ снято" if obj.get("resolved") else "✗ не снято"
            quality = obj.get("handling_quality", 0)
            parts.append(f"• \"{obj.get('text', '')}\" [{resolved}, отработка {quality}/10]")
            if obj.get("broker_response"):
                parts.append(f"  → {obj['broker_response']}")
        parts.append("")

    # Follow-through на предыдущий план
    ft = quality_report.get("previous_recommendations_follow_through") or {}
    if ft.get("total_recommendations", 0) > 0:
        executed = ft.get("executed_count", 0)
        total = ft["total_recommendations"]
        parts.append(f"✅ Выполнено {executed} из {total} рекомендаций прошлого звонка:")
        for item in ft.get("items", []):
            status = item.get("executed", "no")
            icon = {"yes": "✅", "partial": "~", "no": "❌"}.get(status, "—")
            parts.append(f"{icon} {item.get('recommendation_text', '')}")
            if item.get("evidence"):
                parts.append(f"  → {item['evidence']}")
        parts.append("")

    # Оценка аргументации встречи (V4) — отдельный блок, если встреча не закрыта слабо
    maa = quality_report.get("meeting_argumentation_assessment") or {}
    push_quality = maa.get("overall_push_quality")
    missed = maa.get("missed_opportunities") or []
    if push_quality == "weak" and missed:
        parts.append("Аргументация встречи: слабая")
        parts.append("Упущенные возможности:")
        for m in missed[:3]:
            trigger = (m.get("trigger_quote") or "").strip()
            cat = m.get("recommended_category_id") or ""
            why = m.get("why") or ""
            if trigger:
                parts.append(f'• Клиент: «{trigger}»')
            if cat:
                parts.append(f"  → применить категорию `{cat}`")
            if why:
                parts.append(f"  {why}")
        parts.append("")

    # Рекомендации брокеру
    suggestions = quality_report.get("improvement_suggestions", [])
    if suggestions:
        parts.append("Рекомендации:")
        for s in suggestions[:5]:  # Max 5 to keep note concise
            parts.append(f"• {s}")
        parts.append("")

    # Ссылка на подробности
    parts.append(f"Подробнее: {_dashboard_base_url()}/#call/{session_id}")

    return "\n".join(parts).strip()


def format_next_call_plan(plan: dict) -> str:
    """Render a next-call plan dict as markdown-like text for an AmoCRM note."""
    lines: list[str] = ["📋 План следующего звонка (AI)", ""]

    goals = sorted(plan.get("goals") or [], key=lambda g: g.get("priority", 99))
    if goals:
        lines.append("🎯 Цели:")
        for g in goals:
            lines.append(f"{g.get('priority', 1)}. {g.get('text', '')}")
        lines.append("")

    obj = plan.get("unresolved_objections") or []
    if obj:
        lines.append("❗ Открытые возражения:")
        for o in obj:
            pri = o.get("priority", "средний")
            lines.append(f"• «{o.get('text','')}» ({pri} приоритет)")
            if o.get("suggested_response"):
                lines.append(f"  → {o['suggested_response']}")
        lines.append("")

    gaps = plan.get("information_gaps") or []
    if gaps:
        lines.append("🔍 Чего не хватает:")
        for g in gaps:
            field = g.get("field", "")
            why = g.get("why", "")
            lines.append(f"• {field}" + (f" — {why}" if why else ""))
        lines.append("")

    tp = plan.get("talking_points") or []
    if tp:
        lines.append("💬 Аргументы:")
        for t in tp:
            lines.append(f"• {t.get('topic', '')}")
            if t.get("argument"):
                lines.append(f"  {t['argument']}")
            if t.get("personalized_hook"):
                lines.append(f"  ({t['personalized_hook']})")
        lines.append("")

    props = plan.get("recommended_properties") or []
    if props:
        lines.append("🏢 Рекомендуем показать:")
        for p in props:
            lines.append(f"• {p.get('project', '')} — {p.get('reason', '')}")
        lines.append("")

    risks = plan.get("risks") or []
    if risks:
        lines.append("⚠️ Риски:")
        for r in risks:
            lines.append(f"• {r}")
        lines.append("")

    if plan.get("suggested_opener"):
        lines.append("🗣️ Фраза для начала:")
        lines.append(f"«{plan['suggested_opener']}»")

    return "\n".join(lines).strip()


def create_plain_note(lead_id: int, text: str) -> dict:
    """Create a common (non-call) note on a lead. Used for plan notes."""
    if not _get_access_token():
        return {"ok": False, "error": "AmoCRM token not configured"}
    payload = [{"note_type": "common", "params": {"text": text}}]
    try:
        resp = _amo_request(
            "POST",
            f"{AMOCRM_BASE_URL}/api/v4/leads/{lead_id}/notes",
            json=payload,
            timeout=15,
        )
        if resp.status_code == 200:
            note_id = resp.json()["_embedded"]["notes"][0]["id"]
            logger.info(f"Created plain note {note_id} for lead {lead_id}")
            return {"ok": True, "note_id": note_id}
        logger.warning(f"Failed to create plain note: {resp.status_code} {resp.text[:200]}")
        return {"ok": False, "error": resp.text[:200], "status": resp.status_code}
    except Exception:
        logger.exception(f"Error creating plain note for lead {lead_id}")
        return {"ok": False, "error": "Request failed"}


def create_enriched_note(lead_id: int, session_id: str, quality_report: dict,
                         direction: str = "out", duration: int = 0, phone: str = "") -> dict:
    """Создаёт обогащённое примечание-звонок в сделке AmoCRM."""
    if not _get_access_token():
        return {"ok": False, "error": "AmoCRM token not configured"}

    text = format_enriched_note(quality_report, session_id)

    payload = [{
        "note_type": "common",
        "params": {
            "text": text,
        },
    }]

    try:
        resp = _amo_request(
            "POST",
            f"{AMOCRM_BASE_URL}/api/v4/leads/{lead_id}/notes",
            json=payload,
            timeout=15,
        )
        if resp.status_code == 200:
            note_id = resp.json()["_embedded"]["notes"][0]["id"]
            logger.info(f"Created AmoCRM note {note_id} for lead {lead_id}")
            return {"ok": True, "note_id": note_id}
        else:
            logger.warning(f"Failed to create AmoCRM note: {resp.status_code} {resp.text[:200]}")
            return {"ok": False, "error": resp.text[:200], "status": resp.status_code}
    except Exception:
        logger.exception(f"Error creating AmoCRM note for lead {lead_id}")
        return {"ok": False, "error": "Request failed"}


def tag_lead(lead_id: int, tag_name: str):
    """Add a tag to a lead in AmoCRM."""
    if not _get_access_token():
        return
    try:
        resp = _amo_request(
            "PATCH",
            f"{AMOCRM_BASE_URL}/api/v4/leads",
            json=[{"id": lead_id, "_embedded": {"tags": [{"name": tag_name}]}}],
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info(f"Tagged lead {lead_id} with '{tag_name}'")
        else:
            logger.warning(f"Failed to tag lead {lead_id}: {resp.status_code}")
    except Exception:
        logger.exception(f"Error tagging lead {lead_id}")


def delete_note(lead_id: int, note_id: int) -> dict:
    """Delete a single note from a lead in AmoCRM. Idempotent: 404 → ok=True.

    AmoCRM API expects DELETE on /api/v4/leads/{lead_id}/notes/{note_id}.
    Returns {"ok": bool, "already_gone"?: True, "error"?: str}.
    """
    if not _get_access_token():
        return {"ok": False, "error": "AmoCRM token not configured"}
    try:
        resp = _amo_request(
            "DELETE",
            f"{AMOCRM_BASE_URL}/api/v4/leads/{lead_id}/notes/{note_id}",
            timeout=15,
        )
    except Exception:
        logger.exception(f"Error deleting AmoCRM note {note_id}")
        return {"ok": False, "error": "Request failed"}
    if resp.status_code in (200, 202, 204):
        logger.info(f"Deleted AmoCRM note {note_id} from lead {lead_id}")
        return {"ok": True}
    if resp.status_code == 404:
        logger.info(f"AmoCRM note {note_id} on lead {lead_id} already gone (404)")
        return {"ok": True, "already_gone": True}
    logger.warning(f"Failed to delete AmoCRM note {note_id} on lead {lead_id}: {resp.status_code}")
    return {"ok": False, "error": resp.text[:200], "status": resp.status_code}


def update_note(lead_id: int, note_id: int, text: str) -> dict:
    """Обновляет текст существующего примечания."""
    if not _get_access_token():
        return {"ok": False, "error": "AmoCRM token not configured"}

    payload = {"note_type": "common", "params": {"text": text}}
    try:
        resp = _amo_request(
            "PATCH",
            f"{AMOCRM_BASE_URL}/api/v4/leads/{lead_id}/notes/{note_id}",
            json=payload,
            timeout=15,
        )
        if resp.status_code == 200:
            logger.info(f"Updated AmoCRM note {note_id}")
            return {"ok": True}
        else:
            logger.warning(f"Failed to update AmoCRM note {note_id}: {resp.status_code}")
            return {"ok": False, "error": resp.text[:200], "status": resp.status_code}
    except Exception:
        logger.exception(f"Error updating AmoCRM note {note_id}")
        return {"ok": False, "error": "Request failed"}


# === Pipelines / stage resolver ==========================================

import time as _time

_PIPELINES_CACHE: dict | None = None
_PIPELINES_CACHE_TS: float = 0.0
_PIPELINES_TTL_SEC = 3600


def _get_pipelines_cached() -> dict:
    """Return {pipeline_id: {"name": ..., "stages": {status_id: {"name": ..., "id": ...}}}}."""
    global _PIPELINES_CACHE, _PIPELINES_CACHE_TS
    now = _time.time()
    if _PIPELINES_CACHE and now - _PIPELINES_CACHE_TS < _PIPELINES_TTL_SEC:
        return _PIPELINES_CACHE
    if not _get_access_token():
        return _PIPELINES_CACHE or {}
    try:
        resp = _amo_request(
            "GET",
            f"{AMOCRM_BASE_URL}/api/v4/leads/pipelines",
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"Failed to fetch pipelines: {resp.status_code}")
            return _PIPELINES_CACHE or {}
        pipelines: dict = {}
        for p in resp.json().get("_embedded", {}).get("pipelines", []):
            stages = {}
            for s in p.get("_embedded", {}).get("statuses", []):
                stages[s["id"]] = {"name": s.get("name", ""), "id": s["id"]}
            pipelines[p["id"]] = {"name": p.get("name", ""), "stages": stages}
        _PIPELINES_CACHE = pipelines
        _PIPELINES_CACHE_TS = now
        return pipelines
    except Exception:
        logger.exception("Failed to fetch AmoCRM pipelines")
        return _PIPELINES_CACHE or {}


def get_lead_stage(lead_id: int) -> dict | None:
    """Return {"pipeline_name", "stage_name", "stage_id"} for a lead, or None on failure."""
    if not _get_access_token() or not lead_id:
        return None
    try:
        resp = _amo_request(
            "GET",
            f"{AMOCRM_BASE_URL}/api/v4/leads/{lead_id}",
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        pipeline_id = data.get("pipeline_id")
        status_id = data.get("status_id")
        if pipeline_id is None or status_id is None:
            return None
        pipelines = _get_pipelines_cached()
        pipeline = pipelines.get(pipeline_id, {})
        stage = (pipeline.get("stages") or {}).get(status_id, {})
        return {
            "pipeline_name": pipeline.get("name", ""),
            "stage_name": stage.get("name", ""),
            "stage_id": status_id,
        }
    except Exception:
        logger.exception(f"Failed to get stage for lead {lead_id}")
        return None


def fetch_lead_events(lead_id: int, since_ts: int = 0) -> list[dict]:
    """Fetch AmoCRM events (status changes, notes, calls) for a lead since given timestamp."""
    if not _get_access_token() or not lead_id:
        return []
    try:
        resp = _amo_request(
            "GET",
            f"{AMOCRM_BASE_URL}/api/v4/events",
            params={
                "filter[entity]": "lead",
                "filter[entity_id][]": [lead_id],
                "filter[created_at][from]": since_ts,
                "limit": 100,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        return resp.json().get("_embedded", {}).get("events", [])
    except Exception:
        logger.exception(f"Failed to fetch events for lead {lead_id}")
        return []
