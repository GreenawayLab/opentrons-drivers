from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    secret_key: str            # JWT signing
    fernet_key: str            # vault encryption
    algorithm: str = "HS256"
    token_expire_minutes: int = 480


settings = Settings()