from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://realestate:changeme@localhost:5434/realestate"
    REDIS_URL: str = "redis://localhost:6381/0"
    AUDIO_STORAGE_PATH: str = "./data/audio"
    RESULTS_STORAGE_PATH: str = "./data/results"
    SECRET_KEY: str = "change-this-in-production"
    ALLOWED_ORIGINS: str = "*"
    AUTH_USERNAME: str = "admin"
    AUTH_PASSWORD: str = "rogov2025secure"
    # Separate password required to perform destructive actions (e.g. DELETE session).
    # Empty value means destructive actions are disabled (fail-closed).
    DELETE_PASSWORD: str = ""


settings = Settings()
