import logging

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)

# /key/list paging: LiteLLM's largest page (size <= 100), and a cap on pages
# (1,000 keys). Deleted keys stay listed, so a user can pile them up; the cap
# bounds what one page load asks of LiteLLM, and stops a proxy that
# misreports total_pages from looping forever.
KEY_LIST_PAGE_SIZE = 100
KEY_LIST_MAX_PAGES = 10


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


def _extract_team_list(body: object) -> list[dict]:
    """Normalize a LiteLLM team-listing payload into a list of team dicts.

    LiteLLM's ``/team/list`` and ``/team/available`` have returned either a
    bare JSON array or an object wrapping the array under ``teams`` /
    ``available_teams``, depending on version. Accept all of these and
    ignore any non-dict entries.
    """
    if isinstance(body, dict):
        for key in ("teams", "available_teams", "results"):
            value = body.get(key)
            if isinstance(value, list):
                body = value
                break
    if not isinstance(body, list):
        return []
    return [t for t in body if isinstance(t, dict)]


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

    async def ensure_user(
        self, user_id: str, email: str | None = None, username: str | None = None
    ) -> dict:
        """Get or create a LiteLLM user.

        When the user ID isn't the email (OAUTH_USER_ID_FIELD), a new user also
        gets the email as its user_email; otherwise the user is created with
        user_id = email and no user_email, as before. A new user's user_alias,
        the name LiteLLM's admin UI shows, is the username
        (OAUTH_USERNAME_FIELD), else the email when the ID isn't the email:
        the only name such a user has when its email is taken (below).
        """
        existing = await self.get_user(user_id)
        if existing is not None:
            return existing
        alias = username or (email if email and email != user_id else None)
        named = {"user_alias": alias} if alias else {}
        if email and email != user_id:
            try:
                return await self.create_user(user_id, user_email=email, **named)
            except httpx.HTTPStatusError as e:
                # LiteLLM keeps emails unique. Another user with this email (the
                # account was deleted and recreated, so its ID changed, or it was
                # keyed by email before): a new identity gets a new user, just
                # without the email.
                if "already exists" not in e.response.text:
                    raise
                logger.warning(
                    "LiteLLM user with email %s exists under another ID; creating %s without email",
                    email, user_id,
                )
        return await self.create_user(user_id, **named)

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

    async def list_keys(self, user_id: str | None = None, *, team_id: str | None = None) -> dict:
        """GET /key/list?user_id=... (or ?team_id=..., or every key), every page:
        ``{"keys": [...]}``.

        LiteLLM pages the list (10 keys by default), so this asks for its
        largest page, newest first, and follows ``total_pages``. A key that
        shifts onto a later page while paging (a key created meanwhile) is
        returned once. A proxy that doesn't page (no ``total_pages``) is read
        in one request. Past ``KEY_LIST_MAX_PAGES`` the oldest keys are left
        out, with a warning.
        """
        keys: list[dict] = []
        seen: set = set()
        page = 1
        while True:
            params = {
                "return_full_object": "true",
                "page": str(page),
                "size": str(KEY_LIST_PAGE_SIZE),
                "sort_by": "created_at",
                "sort_order": "desc",
            }
            if user_id is not None:
                params["user_id"] = user_id
            if team_id is not None:
                params["team_id"] = team_id
            r = await self._client().get("/key/list", params=params, headers=self._headers())
            r.raise_for_status()
            data = r.json()
            batch = data if isinstance(data, list) else data.get("keys") or []
            for k in batch:
                token = k.get("token") if isinstance(k, dict) else k
                if token is None or token not in seen:
                    seen.add(token)
                    keys.append(k)
            total_pages = data.get("total_pages") if isinstance(data, dict) else None
            if not isinstance(total_pages, int) or page >= total_pages or not batch:
                break
            if page >= KEY_LIST_MAX_PAGES:
                logger.warning(
                    "key list for %s stopped at %d pages of %d (of %d)",
                    user_id or team_id or "all keys", page, KEY_LIST_PAGE_SIZE, total_pages,
                )
                break
            page += 1
        return {"keys": keys}

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

    async def list_teams(self, user_id: str) -> list[dict]:
        """GET /team/list?user_id=... -> teams the user already belongs to.

        LiteLLM has returned this payload as a bare list in some versions
        and as ``{"teams": [...]}`` in others, so normalize both shapes.
        """
        r = await self._client().get(
            "/team/list",
            params={"user_id": user_id},
            headers=self._headers(),
        )
        r.raise_for_status()
        return _extract_team_list(r.json())

    async def list_all_teams(self) -> list[dict]:
        """GET /team/list -> every team (needs an admin key)."""
        r = await self._client().get("/team/list", headers=self._headers())
        r.raise_for_status()
        return _extract_team_list(r.json())

    async def get_team(self, team_id: str) -> dict | None:
        """GET /team/info?team_id=... -> the team record, or None if it doesn't exist.

        Raises on a reply without a ``team_info`` record and its ``models``
        list: read as "no list", that would mean every model on the proxy.
        """
        r = await self._client().get(
            "/team/info", params={"team_id": team_id}, headers=self._headers()
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        body = r.json()
        info = body.get("team_info") if isinstance(body, dict) else None
        if not isinstance(info, dict) or not isinstance(info.get("models"), list):
            raise ValueError("Unexpected /team/info response: no team_info.models list")
        return info

    async def list_models(self) -> list[str]:
        """GET /v1/models -> IDs of every model on the proxy (with the admin key)."""
        r = await self._client().get("/v1/models", headers=self._headers())
        r.raise_for_status()
        data = r.json().get("data", [])
        return sorted({m["id"] for m in data if isinstance(m, dict) and m.get("id")})

    async def list_available_teams(self, user_id: str) -> list[dict]:
        """GET /team/available?user_id=... -> teams the user may join.

        Normalizes the list / ``{"available_teams": [...]}`` / ``{"teams": [...]}``
        shapes LiteLLM has used.
        """
        r = await self._client().get(
            "/team/available",
            params={"user_id": user_id},
            headers=self._headers(),
        )
        r.raise_for_status()
        return _extract_team_list(r.json())

    async def add_team_member(
        self, team_id: str, user_id: str, role: str = "user"
    ) -> dict:
        """POST /team/member_add

        Adds a single member to an existing team, matched by ``user_id``.
        Users are created with ``user_id=<email>`` and no ``user_email``
        (see ``create_user``), and LiteLLM creates a new user row when
        ``member_add`` can't match the member, so matching by ``user_email``
        could attach the team to a duplicate user. ``role`` is one of
        LiteLLM's team roles ("user" or "admin"); callers that expose this to
        end users must keep it pinned to "user" so a user cannot grant
        themselves team-admin.
        """
        body = {
            "team_id": team_id,
            "member": {"user_id": user_id, "role": role},
        }
        r = await self._client().post(
            "/team/member_add",
            json=body,
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    async def ensure_team_member(self, team_id: str, user_id: str) -> bool:
        """Make ``user_id`` a member ("user" role) of ``team_id`` unless it is one.
        True if this call added them.

        Checks membership first (``GET /team/list?user_id=``) rather than reading
        LiteLLM's error text for a repeated add. If the add fails, membership is
        checked again: a concurrent request may have added them meanwhile.
        """
        def member(teams: list[dict]) -> bool:
            return team_id in {t.get("team_id") for t in teams}

        if member(await self.list_teams(user_id)):
            return False
        try:
            await self.add_team_member(team_id, user_id, role="user")
        except httpx.HTTPStatusError:
            if member(await self.list_teams(user_id)):
                return False
            raise
        return True

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
