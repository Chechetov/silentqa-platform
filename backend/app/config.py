from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://realestate:changeme@localhost:5434/realestate"
    REDIS_URL: str = "redis://localhost:6381/0"
    AUDIO_STORAGE_PATH: str = "./data/audio"
    RESULTS_STORAGE_PATH: str = "./data/results"
    SECRET_KEY: str = "change-this-in-production"
    ALLOWED_ORIGINS: str = "*"
    # Dashboard auth (Plan 2)
    SESSION_TTL_SECONDS: int = 7 * 24 * 3600  # 7 days
    SESSION_COOKIE_SECURE: bool = True  # False only for local http:// dev
    LOGIN_RATE_MAX_ATTEMPTS: int = 10
    LOGIN_RATE_WINDOW_SECONDS: int = 300
    LOGIN_RATE_IP_MULTIPLIER: int = 10  # IP-backstop: max_attempts × множитель
    # ISO-дата (UTC) конца grace-окна для брокерских JWT без tenant-claim
    # (спека 5.4: 30 дней от деплоя). Пусто = grace выключен, claim обязателен.
    BROKER_JWT_TENANT_GRACE_UNTIL: str = ""

    # Multi-tenancy
    BASE_DOMAIN: str = "silentqa.com"
    # Fallback tenant slug for hosts that match neither custom_domains nor
    # *.BASE_DOMAIN (local dev: localhost/IP). Empty → platform contour.
    DEFAULT_TENANT: str = ""

    # Лимиты тела запроса, МБ (ревью C-2). Первая линия — глобальный
    # ASGI-middleware по Content-Length; вторая — пер-роутные капы ингеста
    # (по фактическим байтам); внешний слой — request_body в Caddy (ранбук).
    MAX_REQUEST_BODY_MB: int = 600
    MAX_CHUNK_UPLOAD_MB: int = 32    # один 10-секундный WebM/Opus-чанк
    MAX_AUDIO_UPLOAD_MB: int = 512   # файл-аплоуд целого звонка
    # /docs, /redoc, /openapi.json (ревью C-3): по умолчанию выключены,
    # включать только на стейдже/локали.
    ENABLE_API_DOCS: bool = False


settings = Settings()
