from typing import Literal

from pydantic_settings import BaseSettings

# Key metadata the app writes or reads itself (the duration shown in the UI,
# Delete's marker, and "hidden", set by other tools on keys the app leaves
# out): users' metadata can't set it, and REQUIRED_KEY_METADATA can't ask for it.
RESERVED_KEY_METADATA = ("duration", "deleted_at", "hidden")


class Settings(BaseSettings):
    # Core
    DEBUG_MODE: bool = False
    APP_NAME: str = "API Keys"
    # Enable the LiteLLM teams features (self-service team join + team-scoped
    # API keys). Off by default so proxy-only deployments without teams are
    # unaffected; when False the /api/teams routes 404, /api/me omits teams,
    # and the create-key team selector never appears.
    FEATURE_TEAMS_ENABLED: bool = False
    # Where team membership comes from (with FEATURE_TEAMS_ENABLED):
    #   "litellm": LiteLLM's own memberships; people join the teams LiteLLM
    #              lists as available themselves (self-service join)
    #   "claims":  the identity provider: a person's teams are the groups (or
    #              roles) in their sign-in claims that start with
    #              TEAM_GROUP_PREFIX and name a LiteLLM team (its team_alias or
    #              team_id). No self-join; membership is managed in the
    #              identity provider. Needs AUTH_MODE=oauth.
    TEAM_SOURCE: Literal["litellm", "claims"] = "litellm"
    # TEAM_SOURCE=claims: the claim holding the groups (Keycloak: groups;
    # Entra ID: roles, the app roles) and the prefix of those that are teams.
    GROUPS_FIELD: str = "groups"
    TEAM_GROUP_PREFIX: str = "project-"
    # TEAM_SOURCE=claims: groups are re-read (refresh-token grant) before a key
    # is made, changed or unblocked. Without a refresh token the app can't
    # re-read them, so it uses the groups from a sign-in younger than this many
    # seconds, or 60 (team_claims.SIGN_IN_GRACE_SECONDS), whichever is longer;
    # older ones need a new sign-in.
    TEAM_CLAIMS_MAX_AGE_SECONDS: int = 0
    # What the UI and messages call a team, for deployments with their own word
    # for it (e.g. "Project"), and its plural if adding an "s" isn't it (e.g.
    # "Classes"). API field names stay team_*.
    TEAM_LABEL: str = "Team"
    TEAM_LABEL_PLURAL: str = ""
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
    # Field in the userinfo response (or ID token claim) that holds the user's email
    OAUTH_EMAIL_FIELD: str = "email"
    # Where the user's claims come from:
    #   "userinfo": the provider's userinfo endpoint (OAUTH_USERINFO_URL)
    #   "id_token": the OpenID Connect ID token from the token response,
    #               verified against the provider's signing keys (OAUTH_ISSUER)
    OAUTH_CLAIMS_SOURCE: Literal["userinfo", "id_token"] = "userinfo"
    # OpenID Connect issuer: the ID token's expected "iss", and where the
    # signing keys are discovered (<issuer>/.well-known/openid-configuration).
    # Required with OAUTH_CLAIMS_SOURCE=id_token.
    OAUTH_ISSUER: str = ""
    # Signing keys (JWKS) URL, if not the one discovery names; e.g. an
    # in-cluster address for a provider whose public URL the app can't reach.
    OAUTH_JWKS_URL: str = ""
    # Claim that identifies the user in LiteLLM (its user_id), e.g. "sub" or
    # Entra's "oid". Empty: the email, as before. Set it when emails can
    # change or be reused; the email is then stored as the user's email.
    OAUTH_USER_ID_FIELD: str = ""

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
    # Longest duration a key may be given, as a LiteLLM duration (e.g. "90d",
    # "3mo"; a month counts as 30 days). When set, a key created without a duration gets this one, and a
    # create or update asking for a longer one is rejected. It caps each
    # period, not a key's total life: an update with a duration starts a new
    # period, so an owner can renew a key (README). Empty = no limit (keys
    # without a duration never expire).
    MAX_KEY_DURATION: str = ""
    # Comma-separated list of required metadata field names per key
    # e.g. "project,task_number"
    REQUIRED_KEY_METADATA: str = ""
    # Show the "Spend" column in the keys table. Set to False for
    # deployments where spend tracking isn't meaningful and the column
    # would just add noise.
    SHOW_SPEND_COLUMN: bool = True
    # Show the "Budget" column in the keys table. Independent from the
    # Spend column so deployments can surface either, both, or neither.
    SHOW_BUDGET_COLUMN: bool = True
    # Show the project/task usage breakdown on the usage dashboard, and offer
    # an optional Task (task metadata) when creating a key.
    SHOW_PROJECT_TASK_BREAKDOWN: bool = True
    # Show key names without the "<user>-" prefix every key name starts with
    # (display only: the key's alias in LiteLLM keeps it).
    HIDE_KEY_NAME_PREFIX: bool = True
    # TEAM_SOURCE=claims: show team names without TEAM_GROUP_PREFIX
    # ("project-alpha" as "alpha"), in the keys table, the team pickers and the
    # Usage page (display only).
    HIDE_TEAM_NAME_PREFIX: bool = False

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
    def project_is_team(self) -> bool:
        """Whether the Usage page's project/task breakdown takes a key's project
        from its team: with teams, unless REQUIRED_KEY_METADATA asks people for
        a project of their own (a charge code, say), which it then groups by."""
        return self.FEATURE_TEAMS_ENABLED and "project" not in self.required_metadata_fields

    @property
    def optional_task(self) -> bool:
        """Whether the key form offers an optional Task (``task`` metadata,
        free text): with the Usage page's project/task breakdown, unless
        REQUIRED_KEY_METADATA already asks for a task (``task`` or
        ``task_number``)."""
        return self.SHOW_PROJECT_TASK_BREAKDOWN and not (
            {"task", "task_number"} & set(self.required_metadata_fields)
        )

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

    def team_label(self, plural: bool = False) -> str:
        """TEAM_LABEL as written ("Project"), or its plural: TEAM_LABEL_PLURAL, else
        the label with an "s". A blank label is "Team"."""
        label = self.TEAM_LABEL.strip() or "Team"
        if not plural:
            return label
        return self.TEAM_LABEL_PLURAL.strip() or label + "s"

    def team_word(self, plural: bool = False) -> str:
        """The label within a sentence: lower case if only its first letter is a
        capital ("projects"); otherwise as written ("SIGs", "Cost Center")."""
        word = self.team_label(plural)
        return word.lower() if word[1:] == word[1:].lower() else word

    def team_name_prefix(self) -> str:
        """The prefix the page leaves off team names: TEAM_GROUP_PREFIX, with
        HIDE_TEAM_NAME_PREFIX and teams from the identity provider
        (TEAM_SOURCE=claims), whose groups all have it; else none. Teams people
        join in LiteLLM have names of any kind: without the prefix, "project-x"
        and "x" could look the same."""
        hide = self.HIDE_TEAM_NAME_PREFIX and self.TEAM_SOURCE == "claims"
        return self.TEAM_GROUP_PREFIX if hide else ""

    def team_display_name(self, name: str) -> str:
        """A team's name as shown, without team_name_prefix(). A name without
        it, or with nothing after it, is shown as it is."""
        prefix = self.team_name_prefix()
        if prefix and name.startswith(prefix) and name != prefix:
            return name[len(prefix):]
        return name

    def key_display_name(self, name: str, user_email: str) -> str:
        """A key's name as shown: without the "<user>-" prefix the app puts on
        every key name, with HIDE_KEY_NAME_PREFIX. Names without it (keys made
        outside the app) are shown as they are."""
        prefix = f"{user_email}-"
        if self.HIDE_KEY_NAME_PREFIX and user_email and name.startswith(prefix) and name != prefix:
            return name[len(prefix):]
        return name

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
