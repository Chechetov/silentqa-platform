from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://realestate:changeme@localhost:5434/realestate"
    REDIS_URL: str = "redis://localhost:6381/0"
    AUDIO_STORAGE_PATH: str = "./data/audio"
    RESULTS_STORAGE_PATH: str = "./data/results"
    SECRET_KEY: str = "change-this-in-production"
    ALLOWED_ORIGINS: str = "*"
    # interim gate for /api/companies until Plan 3
    AUTH_USERNAME: str = "admin"
    AUTH_PASSWORD: str = "rogov2025secure"

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


settings = Settings()
