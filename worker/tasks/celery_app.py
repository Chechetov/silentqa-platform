import os
from celery import Celery

redis_url = os.getenv("REDIS_URL", "redis://localhost:6381/0")

app = Celery(
    "voiceqa",
    broker=redis_url,
    backend=redis_url,
    include=[
        "tasks.pipeline",
        "tasks.transcribe",
        "tasks.diarize",
        "tasks.sentiment",
        "tasks.quality",
        "tasks.amocrm_poll",
        "tasks.amocrm_reconcile",
        "tasks.session_watchdog",
    ],
)

app.conf.update(
    # Сериализация
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Очереди
    task_default_queue="default",
    task_queues={
        "default": {"exchange": "default", "routing_key": "default"},
        "transcription": {"exchange": "transcription", "routing_key": "transcription"},
    },
    # Таймауты — обработка часового звонка может занять 30+ минут
    task_soft_time_limit=3600,   # 60 мин soft limit
    task_time_limit=5400,        # 90 мин hard limit
    # Память — перезапуск воркера после N задач (защита от утечек)
    worker_max_tasks_per_child=10,
    # Prefetch — не брать следующую задачу пока текущая не завершена
    # Важно для тяжёлых ML задач!
    worker_prefetch_multiplier=1,
    # Celery Beat — periodic tasks
    beat_schedule={
        "poll-amocrm-calls": {
            "task": "amocrm_poll.poll_amocrm_calls",
            "schedule": 300,  # every 5 minutes
        },
        "sweep-stuck-sessions": {
            "task": "session_watchdog.sweep_stuck_sessions",
            "schedule": 300,  # every 5 minutes
        },
        # Self-healing backstop: deep-sweep for calls the 5-min poll missed
        # (late events-API events) + stuck-call detection. Slower second tier.
        "reconcile-amocrm-calls": {
            "task": "amocrm_reconcile.reconcile_amocrm_calls",
            "schedule": int(os.getenv("AMOCRM_RECONCILE_INTERVAL_SEC", "1800")),  # 30 min
        },
    },
)
