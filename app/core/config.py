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
    # Timeouts (seconds) for outbound calls to LiteLLM
    LITELLM_CONNECT_TIMEOUT: float = 2.0
    LITELLM_READ_TIMEOUT: float = 10.0

    # Auth mode: "proxy" (header injection from reverse proxy) or "oauth"
    AUTH_MODE: Literal["proxy", "oauth"] = "proxy"

    # Reverse proxy auth
    AUTH_USER_HEADER: str = "X-User-Email"
    TEST_USER: str = "test@test.com"
    PROXY_SECRET_HEADER: str = "X-Proxy-Secret"
    PROXY_SECRET: str = ""
    # Require a shared proxy secret by default. Only disable for local dev.
    FEATURE_PROXY_SECRET_ENABLED: bool = True
    STRIP_USER_DOMAIN: bool = False
    # When True, unauthenticated requests fall back to TEST_USER. This is
    # an explicit, dangerous development-only opt-in. Decoupled from
    # DEBUG_MODE so a single misconfigured env var cannot disable auth.
    ALLOW_TEST_USER: bool = False
    # Escape hatch to allow starting in insecure proxy-mode configurations
    # (e.g. for local tooling). Never enable in production.
    ALLOW_INSECURE_STARTUP: bool = False

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
    # Idle timeout in seconds (session evicted if unused). Default 1 day.
    SESSION_IDLE_TIMEOUT: int = 60 * 60 * 24
    # Require HTTPS for the session cookie. Set to false when running behind
    # a TLS-terminating proxy/ingress (e.g. in Kubernetes) where the app
    # itself only sees plain HTTP traffic.
    SESSION_COOKIE_SECURE: bool = True

    # Rate limits (fixed window, in-memory)
    RATE_LIMIT_API_PER_MINUTE: int = 120
    RATE_LIMIT_KEY_CREATE_PER_HOUR: int = 30
    RATE_LIMIT_LOGIN_PER_MINUTE: int = 10

    # Key policy
    MAX_ACTIVE_KEYS_PER_USER: int | None = None
    # Comma-separated list of required metadata field names per key
    # e.g. "project,task_number"
    REQUIRED_KEY_METADATA: str = ""
    # Show the "Spend" column in the keys table. Set to False for
    # deployments where spend tracking isn't meaningful and the column
    # would just add noise.
    SHOW_SPEND_COLUMN: bool = True

    # Custom navigation links shown in the page header. Format is a
    # comma-separated list of "Name|URL" pairs, e.g.
    #   "Docs|https://docs.example.com,Support|https://support.example.com"
    # The "|" separator is used (instead of ":" or "=") so URLs containing
    # those characters (schemes, query strings) don't need escaping.
    NAV_LINKS: str = ""

    # Comma-separated list of origins (scheme://host[:port]) that are
    # allowed for cross-origin-looking state-changing requests in OAuth
    # mode. When the app runs behind a TLS-terminating reverse proxy or
    # Kubernetes ingress, the browser's Origin header reflects the public
    # URL (e.g. "https://app.example.com") while the app itself only sees
    # plain HTTP on an internal host. Without this setting the built-in
    # CSRF same-origin check would compare those two and reject every
    # write as "Cross-origin request denied". List the public origins the
    # app is reachable on, e.g.
    #   "https://app.example.com,https://app-staging.example.com"
    # Trailing slashes and paths are ignored. When empty, the app falls
    # back to a strict same-origin comparison against its own request URL.
    TRUSTED_ORIGINS: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def required_metadata_fields(self) -> list[str]:
        if not self.REQUIRED_KEY_METADATA:
            return []
        return [
            f.strip() for f in self.REQUIRED_KEY_METADATA.split(",") if f.strip()
        ]

    @property
    def nav_links(self) -> list[dict[str, str]]:
        """Parse NAV_LINKS into a list of {name, url} dicts.

        Entries without a "|" separator, or with an empty name or URL, are
        silently skipped so a malformed entry can't break the UI.
        """
        if not self.NAV_LINKS:
            return []
        links: list[dict[str, str]] = []
        for raw in self.NAV_LINKS.split(","):
            entry = raw.strip()
            if not entry or "|" not in entry:
                continue
            name, url = entry.split("|", 1)
            name = name.strip()
            url = url.strip()
            if name and url:
                links.append({"name": name, "url": url})
        return links

    @property
    def oauth_scope_list(self) -> list[str]:
        return [s for s in self.OAUTH_SCOPES.split() if s]

    @property
    def trusted_origins(self) -> list[str]:
        """Parse TRUSTED_ORIGINS into a normalized list of origins.

        Each entry is lowercased and stripped of any path/query/fragment
        so comparisons against a browser-sent Origin header are exact
        (scheme + host + optional port only). Malformed entries without
        a scheme are silently skipped so a typo can't accidentally
        widen the allow-list.
        """
        raw = self.TRUSTED_ORIGINS or ""
        out: list[str] = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            # Require an explicit scheme so we never match "*.example.com"
            # against a maliciously-crafted Origin: null or similar.
            if "://" not in entry:
                continue
            from urllib.parse import urlparse
            parsed = urlparse(entry)
            if not parsed.scheme or not parsed.hostname:
                continue
            netloc = parsed.hostname.lower()
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            out.append(f"{parsed.scheme.lower()}://{netloc}")
        return out

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
