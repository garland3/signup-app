from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Core
    DEBUG_MODE: bool = False
    APP_NAME: str = "API Keys"
    # Optional URL path prefix the app is served under (e.g. "/start").
    # When set, all routes, static assets, and auth redirects are served
    # beneath this prefix. Leave blank to serve from the site root.
    ROOT_PATH: str = ""

    # LiteLLM proxy connection
    LITELLM_BASE_URL: str = "http://localhost:4000"
    LITELLM_ADMIN_KEY: str = ""

    # Auth mode: "proxy" (header injection from reverse proxy) or "oauth"
    AUTH_MODE: Literal["proxy", "oauth"] = "proxy"

    # Reverse proxy auth
    AUTH_USER_HEADER: str = "X-User-Email"
    TEST_USER: str = "test@test.com"
    PROXY_SECRET_HEADER: str = "X-Proxy-Secret"
    PROXY_SECRET: str = ""
    FEATURE_PROXY_SECRET_ENABLED: bool = False
    STRIP_USER_DOMAIN: bool = False

    # OAuth 2.0 / OIDC provider settings (used when AUTH_MODE=oauth)
    OAUTH_CLIENT_ID: str = ""
    OAUTH_CLIENT_SECRET: str = ""
    OAUTH_AUTHORIZE_URL: str = ""
    OAUTH_TOKEN_URL: str = ""
    OAUTH_USERINFO_URL: str = ""
    OAUTH_SCOPES: str = "openid email profile"
    # Full callback URL registered with the provider, e.g.
    # https://app.example.com/api/auth/callback
    OAUTH_REDIRECT_URL: str = ""
    # Field in the userinfo response that holds the user's email
    OAUTH_EMAIL_FIELD: str = "email"

    # Session cookie (used by oauth mode)
    SESSION_SECRET: str = ""
    SESSION_COOKIE_NAME: str = "signup_session"
    # Max age of session cookie in seconds (default 7 days)
    SESSION_MAX_AGE: int = 60 * 60 * 24 * 7
    # Require HTTPS for the session cookie. Set to false when running behind
    # a TLS-terminating proxy/ingress (e.g. in Kubernetes) where the app
    # itself only sees plain HTTP traffic.
    SESSION_COOKIE_SECURE: bool = True

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

    @property
    def oauth_scope_list(self) -> list[str]:
        return [s for s in self.OAUTH_SCOPES.split() if s]

    @property
    def normalized_root_path(self) -> str:
        """Return ROOT_PATH normalized to either "" or "/prefix" (no trailing slash)."""
        p = (self.ROOT_PATH or "").strip()
        if not p:
            return ""
        if not p.startswith("/"):
            p = "/" + p
        return p.rstrip("/") or ""


settings: Settings | None = None


def get_settings() -> Settings:
    global settings
    if settings is None:
        settings = Settings()
    return settings
