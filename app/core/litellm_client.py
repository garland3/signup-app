import httpx

from app.core.config import Settings


class LiteLLMClient:
    """Client for LiteLLM proxy admin API. Uses the master key for auth."""

    def __init__(self, settings: Settings):
        self.base_url = settings.LITELLM_BASE_URL.rstrip("/")
        self.admin_key = settings.LITELLM_ADMIN_KEY

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.admin_key}",
            "Content-Type": "application/json",
        }

    async def generate_key(
        self, user_id: str, key_alias: str, **kwargs
    ) -> dict:
        """POST /key/generate"""
        body = {"user_id": user_id, "key_alias": key_alias, **kwargs}
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base_url}/key/generate",
                json=body,
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    async def list_keys(self, user_id: str) -> dict:
        """GET /key/list?user_id=..."""
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{self.base_url}/key/list",
                params={"user_id": user_id, "return_full_object": "true"},
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    async def get_key_info(self, key: str) -> dict:
        """GET /key/info?key=..."""
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{self.base_url}/key/info",
                params={"key": key},
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    async def update_key(self, key: str, **kwargs) -> dict:
        """POST /key/update"""
        body = {"key": key, **kwargs}
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base_url}/key/update",
                json=body,
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    async def delete_key(self, keys: list[str]) -> dict:
        """POST /key/delete"""
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base_url}/key/delete",
                json={"keys": keys},
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    async def block_key(self, key: str) -> dict:
        """POST /key/block"""
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base_url}/key/block",
                json={"key": key},
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    async def unblock_key(self, key: str) -> dict:
        """POST /key/unblock"""
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base_url}/key/unblock",
                json={"key": key},
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()
