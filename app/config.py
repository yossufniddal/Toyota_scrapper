from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Toyota Scrapper"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000

    anthropic_api_key: str
    claude_model: str = "claude-haiku-4-5"


settings = Settings()
