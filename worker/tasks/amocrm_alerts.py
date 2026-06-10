"""Lightweight alerting for the AmoCRM ingestion self-healing layer.

Sends a Telegram message via the shared pbn-leads bot when configured; if the
TELEGRAM_* env vars are absent (e.g. dev), it degrades to a WARNING log and
never raises — alerting must never break the task that calls it. Env is read at
import, so changing it requires a worker restart (same as the other AmoCRM knobs).
"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Optional dedicated chat for AmoCRM alerts; otherwise reuse the shared chat.
TELEGRAM_CHAT_ID = os.getenv("AMOCRM_ALERT_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", "")

_PREFIX = "\U0001F4DE AmoCRM reconcile"


def send_alert(text: str) -> bool:
    """Best-effort alert. Returns True only if a Telegram message was sent.

    When Telegram is not configured the message is logged at WARNING so it is
    still visible to log-based monitoring; the caller treats the return value
    as advisory and continues regardless.
    """
    msg = f"{_PREFIX}: {text}"
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        logger.warning(f"[alert] {msg}")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"[alert] telegram returned {resp.status_code}: {msg}")
            return False
        return True
    except Exception:
        logger.exception(f"[alert] telegram send failed (continuing): {msg}")
        return False
