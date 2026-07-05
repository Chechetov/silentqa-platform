"""Риск-алерты дашборда (Telegram). Деградирует в WARNING-лог без env; никогда не raise.

Отличие от amocrm_alerts: env читается на вызове (тестируемость), chat_id
пер-тенантный — company-config alerts.telegram_chat_id → RISK_ALERT_CHAT_ID
→ TELEGRAM_CHAT_ID."""
import logging
import os

import requests

logger = logging.getLogger(__name__)

FLAG_RU = {
    "low_score": "низкая оценка",
    "negative_sentiment": "негативная тональность",
    "unresolved_objections": "неотработанные возражения",
}


def _post_telegram(token: str, chat_id: str, text: str) -> bool:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text}, timeout=10)
    return resp.status_code == 200


def send_risk_alert(*, slug: str | None, session_id: str, employee: str | None,
                    score, flags: list[str], company_config: dict | None = None) -> bool:
    reasons = ", ".join(FLAG_RU.get(f, f) for f in flags)
    who = employee or "менеджер не указан"
    score_txt = f"{score:g}/10" if score is not None else "без оценки"
    url = f"https://{slug}.silentqa.com/#call/{session_id}" if slug else f"#call/{session_id}"
    msg = f"⚠️ SilentQA [{slug or '?'}]: рисковый звонок — {who}, оценка {score_txt} ({reasons})\n{url}"

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = (((company_config or {}).get("alerts") or {}).get("telegram_chat_id")
               or os.getenv("RISK_ALERT_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", ""))
    if not (token and chat_id):
        logger.warning(f"[alert] {msg}")
        return False
    try:
        if _post_telegram(token, chat_id, msg):
            return True
        logger.warning(f"[alert] telegram не 200: {msg}")
        return False
    except Exception:
        logger.exception(f"[alert] telegram send failed (continuing): {msg}")
        return False
