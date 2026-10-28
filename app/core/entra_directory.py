"""Who is in a project's group or app role now, from Microsoft Graph.

Used by the key reconciler (key_reconciler.py), with the app's own Entra ID
credentials (client credentials grant). A group is found by its object ID (what
Entra puts in the groups claim) or, failing that, its exact display name; an app
role by its value on the sign-in application (OAUTH_CLIENT_ID).

``None`` means "not found": the caller then changes nothing for that project.
Anything Graph can't answer raises DirectoryError, with the same effect.
"""
from __future__ import annotations

import re
import time

import httpx

from app.core.config import Settings

GUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
USER_FIELDS = "id,mail,userPrincipalName,accountEnabled"
MAX_PAGES = 200  # 999 a page: far more members than any project group has


class DirectoryError(Exception):
    pass


class EntraDirectory:
    def __init__(self, settings: Settings):
        self.tenant = settings.GRAPH_TENANT_ID
        self.client_id = settings.GRAPH_CLIENT_ID or settings.OAUTH_CLIENT_ID
        self.client_secret = settings.GRAPH_CLIENT_SECRET or settings.OAUTH_CLIENT_SECRET
        self.app_id = settings.OAUTH_CLIENT_ID  # the sign-in app, whose roles count
        self.graph = settings.GRAPH_BASE_URL.rstrip("/")
        self.login = settings.GRAPH_LOGIN_URL.rstrip("/")
        self._http: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_until = 0.0

    async def __aenter__(self) -> "EntraDirectory":
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        return self

    async def __aexit__(self, *exc) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def _bearer(self) -> str:
        if self._token and time.monotonic() < self._token_until:
            return self._token
        r = await self._http.post(
            f"{self.login}/{self.tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": self.graph.rsplit("/", 1)[0] + "/.default",
            },
        )
        if r.status_code != 200:
            raise DirectoryError(f"Graph token request failed: HTTP {r.status_code}")
        body = r.json()
        self._token = body["access_token"]
        self._token_until = time.monotonic() + max(60, int(body.get("expires_in", 3600)) - 60)
        return self._token

    async def _get(self, url: str, params: dict | None = None) -> dict | None:
        r = await self._http.get(
            url, params=params, headers={"Authorization": f"Bearer {await self._bearer()}"}
        )
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise DirectoryError(f"Graph GET {url.split('?')[0]} failed: HTTP {r.status_code}")
        return r.json()

    async def _get_all(self, path: str, params: dict) -> list[dict] | None:
        """Every page of a Graph collection (``value``, ``@odata.nextLink``)."""
        url: str | None = self.graph + path
        out: list[dict] = []
        for _ in range(MAX_PAGES):
            body = await self._get(url, params)
            if body is None:
                return None
            out.extend(v for v in body.get("value", []) if isinstance(v, dict))
            url, params = body.get("@odata.nextLink"), None
            if not url:
                return out
        raise DirectoryError(f"Graph {path}: more than {MAX_PAGES} pages")

    async def group_members(self, group: str) -> list[dict] | None:
        """The users in a group (nested groups included), by object ID or name."""
        if not GUID.match(group):
            found = await self._get_all(
                "/groups",
                {"$filter": "displayName eq '%s'" % group.replace("'", "''"), "$select": "id"},
            )
            if not found or len(found) != 1:  # missing, or a name two groups share
                return None
            group = found[0]["id"]
        return await self._get_all(
            f"/groups/{group}/transitiveMembers/microsoft.graph.user",
            {"$select": USER_FIELDS, "$top": "999"},
        )

    async def app_role_members(self, value: str) -> list[dict] | None:
        """The users holding the sign-in app's role ``value``: assigned directly,
        or through an assigned group."""
        sps = await self._get_all(
            "/servicePrincipals", {"$filter": f"appId eq '{self.app_id}'", "$select": "id,appRoles"}
        )
        if not sps or len(sps) != 1:
            return None
        role_ids = {
            r.get("id") for r in sps[0].get("appRoles") or []
            if r.get("value") == value and r.get("isEnabled", True)
        }
        if not role_ids:
            return None
        assignments = await self._get_all(
            f"/servicePrincipals/{sps[0]['id']}/appRoleAssignedTo",
            {"$select": "principalId,principalType,appRoleId", "$top": "999"},
        )
        if assignments is None:
            return None
        members: list[dict] = []
        for a in assignments:
            if a.get("appRoleId") not in role_ids:
                continue
            if a.get("principalType") == "Group":
                users = await self.group_members(a["principalId"])
                if users is None:  # an assigned group that can't be read: unsure
                    raise DirectoryError(f"group {a['principalId']} assigned to {value} not found")
                members.extend(users)
            elif a.get("principalType") == "User":
                user = await self._get(f"{self.graph}/users/{a['principalId']}", {"$select": USER_FIELDS})
                if user is not None:
                    members.append(user)
        return members
