from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Core
    DEBUG_MODE: bool = False
    APP_NAME: str = "API Keys"

    # LiteLLM proxy connection
    LITELLM_BASE_URL: str = "http://localhost:4000"
    LITELLM_ADMIN_KEY: str = ""

    # Auth
    AUTH_USER_HEADER: str = "X-User-Email"
    TEST_USER: str = "test@test.com"
    PROXY_SECRET_HEADER: str = "X-Proxy-Secret"
    PROXY_SECRET: str = ""
    FEATURE_PROXY_SECRET_ENABLED: bool = False
    STRIP_USER_DOMAIN: bool = False

    # Key policy
    MAX_ACTIVE_KEYS_PER_USER: int | None = None
    # Comma-separated list of required metadata field names per key
    # e.g. "project,task_number"
    REQUIRED_KEY_METADATA: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def required_metadata_fields(self) -> list[str]:
        if not self.REQUIRED_KEY_METADATA:
            return []
        return [
            f.strip() for f in self.REQUIRED_KEY_METADATA.split(",") if f.strip()
        ]


settings: Settings | None = None


def get_settings() -> Settings:
    global settings
    if settings is None:
        settings = Settings()
    return settings
