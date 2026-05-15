import logging

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


class DuplicateKeyAliasError(Exception):
    """Raised when LiteLLM rejects a key because the alias is already taken.

    LiteLLM's ``/key/generate`` endpoint enforces uniqueness on
    ``key_alias`` and returns a 400 with a message like
    ``"Unique key aliases are required. Key alias=... already exists."``.
    We surface that as a typed exception so the route layer can translate
    it into a user-facing 409 with a "pick a different name" prompt
    instead of the generic 502 used for other upstream failures.
    """

    def __init__(self, alias: str = ""):
        self.alias = alias
        super().__init__(f"Key alias already exists: {alias!r}")


def _is_duplicate_alias_error(body: object) -> bool:
    """Return True when a LiteLLM 400 response body describes a duplicate alias.

    Checks both the ``{"error": {"message": ...}}`` shape used by the real
    LiteLLM proxy exception handler and the simpler ``{"detail": ...}``
    shape FastAPI produces directly, so the route behaves identically
    against the real proxy and the test mock.
    """
    if not isinstance(body, dict):
        return False
    message = ""
    err = body.get("error")
    if isinstance(err, dict):
        message = str(err.get("message") or "")
    if not message:
        message = str(body.get("detail") or body.get("message") or "")
    msg = message.lower()
    if "alias" not in msg:
        return False
    return "already exist" in msg or "unique" in msg


class LiteLLMClient:
    """Client for LiteLLM proxy admin API. Uses the master key for auth.

    A single :class:`httpx.AsyncClient` is shared across calls to benefit
    from connection pooling and to enforce consistent timeouts.
    """

    _shared: dict[str, httpx.AsyncClient] = {}

    def __init__(self, settings: Settings):
        self.base_url = settings.LITELLM_BASE_URL.rstrip("/")
        self.admin_key = settings.LITELLM_ADMIN_KEY
        self._connect_timeout = settings.LITELLM_CONNECT_TIMEOUT
        self._read_timeout = settings.LITELLM_READ_TIMEOUT

    def _client(self) -> httpx.AsyncClient:
        client = self._shared.get(self.base_url)
        if client is None or client.is_closed:
            timeout = httpx.Timeout(
                connect=self._connect_timeout,
                read=self._read_timeout,
                write=self._read_timeout,
                pool=self._connect_timeout,
            )
            client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=timeout,
                limits=httpx.Limits(
                    max_connections=50, max_keepalive_connections=20
                ),
            )
            self._shared[self.base_url] = client
        return client

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.admin_key}",
            "Content-Type": "application/json",
        }

    async def get_user(self, user_id: str) -> dict | None:
        """GET /user/info?user_id=...  Returns None if the user doesn't exist."""
        r = await self._client().get(
            "/user/info",
            params={"user_id": user_id},
            headers=self._headers(),
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def create_user(self, user_id: str, **kwargs) -> dict:
        """POST /user/new"""
        body = {
            "user_id": user_id,
            "user_role": "internal_user",
            "send_invite_email": False,
            "auto_create_key": False,
            **kwargs,
        }
        r = await self._client().post(
            "/user/new",
            json=body,
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    async def ensure_user(self, user_id: str) -> dict:
        """Get or create a LiteLLM user."""
        existing = await self.get_user(user_id)
        if existing is not None:
            return existing
        return await self.create_user(user_id)

    async def generate_key(
        self, user_id: str, key_alias: str, **kwargs
    ) -> dict:
        """POST /key/generate

        Raises :class:`DuplicateKeyAliasError` when LiteLLM rejects the
        request because the alias is already in use. Other upstream
        failures surface as the usual ``httpx.HTTPStatusError``.
        """
        body = {"user_id": user_id, "key_alias": key_alias, **kwargs}
        r = await self._client().post(
            "/key/generate",
            json=body,
            headers=self._headers(),
        )
        if r.status_code == 400:
            try:
                body_json = r.json()
            except ValueError:
                body_json = None
            if _is_duplicate_alias_error(body_json):
                raise DuplicateKeyAliasError(alias=key_alias)
        r.raise_for_status()
        return r.json()

    async def list_keys(self, user_id: str) -> dict:
        """GET /key/list?user_id=..."""
        r = await self._client().get(
            "/key/list",
            params={"user_id": user_id, "return_full_object": "true"},
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    async def get_key_info(self, key: str) -> dict:
        """GET /key/info?key=..."""
        r = await self._client().get(
            "/key/info",
            params={"key": key},
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    async def update_key(self, key: str, **kwargs) -> dict:
        """POST /key/update"""
        body = {"key": key, **kwargs}
        r = await self._client().post(
            "/key/update",
            json=body,
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    async def delete_key(self, keys: list[str]) -> dict:
        """POST /key/delete"""
        r = await self._client().post(
            "/key/delete",
            json={"keys": keys},
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    async def block_key(self, key: str) -> dict:
        """POST /key/block"""
        r = await self._client().post(
            "/key/block",
            json={"key": key},
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    async def unblock_key(self, key: str) -> dict:
        """POST /key/unblock"""
        r = await self._client().post(
            "/key/unblock",
            json={"key": key},
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    async def get_user_info(self, user_id: str) -> dict | None:
        """GET /user/info?user_id=...

        Returns the full user record (budget, spend totals, attached
        keys/teams). Distinct from :meth:`get_user` only in name; kept as
        a separate method so the dashboard route can be wired without
        accidentally inheriting key-management call sites.
        """
        r = await self._client().get(
            "/user/info",
            params={"user_id": user_id},
            headers=self._headers(),
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def get_user_daily_activity(
        self,
        user_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
        page_size: int = 1000,
    ) -> dict:
        """GET /user/daily/activity?user_id=...

        Returns the pre-aggregated daily activity for ``user_id`` — one
        row per day with per-model and per-api_key breakdowns already
        rolled up by the proxy. This is the same endpoint LiteLLM's
        admin UI uses for its per-user spend view, and it's dramatically
        cheaper than ``/spend/logs?summarize=false`` because the proxy
        reads from the ``LiteLLM_DailyUserSpend`` table instead of the
        raw per-request log.

        The response is paginated; we follow ``has_more`` and merge
        ``results`` from every page. ``page_size`` is capped at 1000 by
        the proxy. For a single user 365 daily rows fit comfortably
        in one page in the common case, but we page defensively.
        """
        params: dict[str, str] = {
            "user_id": user_id,
            "page_size": str(page_size),
        }
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

        merged_results: list[dict] = []
        merged_metadata: dict = {}
        page = 1
        while True:
            params["page"] = str(page)
            r = await self._client().get(
                "/user/daily/activity",
                params=params,
                headers=self._headers(),
            )
            if r.status_code == 404:
                return {"results": [], "metadata": {}}
            r.raise_for_status()
            body = r.json()
            if not isinstance(body, dict):
                return {"results": [], "metadata": {}}
            results = body.get("results") or []
            if isinstance(results, list):
                merged_results.extend(results)
            metadata = body.get("metadata") or {}
            if isinstance(metadata, dict):
                merged_metadata = metadata
            has_more = bool(metadata.get("has_more")) if isinstance(metadata, dict) else False
            if not has_more:
                break
            page += 1
            # Hard cap to keep a malformed has_more flag from spinning forever.
            if page > 50:
                break
        return {"results": merged_results, "metadata": merged_metadata}
