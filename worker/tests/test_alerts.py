"""Риск-алерты: деградация без env, порядок chat_id, формат сообщения."""
import tasks.alerts as alerts


def test_no_env_degrades_to_log(monkeypatch, caplog):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert alerts.send_risk_alert(
        slug="acme", session_id="sid", employee="Иванов",
        score=2, flags=["low_score"]) is False


def test_chat_id_priority_config_over_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("RISK_ALERT_CHAT_ID", "env-chat")
    sent = {}
    monkeypatch.setattr(alerts, "_post_telegram",
                        lambda token, chat_id, text: sent.update(chat=chat_id, text=text) or True)
    alerts.send_risk_alert(slug="acme", session_id="sid", employee=None, score=3,
                           flags=["low_score"],
                           company_config={"alerts": {"telegram_chat_id": "cfg-chat"}})
    assert sent["chat"] == "cfg-chat"


def test_message_format_russian(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    sent = {}
    monkeypatch.setattr(alerts, "_post_telegram",
                        lambda token, chat_id, text: sent.update(text=text) or True)
    alerts.send_risk_alert(slug="acme", session_id="sid-9", employee="Иванов",
                           score=2, flags=["low_score", "unresolved_objections"])
    assert "рисковый звонок" in sent["text"].lower()
    assert "Иванов" in sent["text"] and "2/10" in sent["text"]
    assert "https://acme.silentqa.com/#call/sid-9" in sent["text"]
    assert "низкая оценка" in sent["text"] and "неотработанные возражения" in sent["text"]


def test_post_failure_never_raises(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    def boom(*a):
        raise RuntimeError("net")
    monkeypatch.setattr(alerts, "_post_telegram", boom)
    assert alerts.send_risk_alert(slug="a", session_id="s", employee=None,
                                  score=None, flags=["negative_sentiment"]) is False
