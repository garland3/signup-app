"""OpenID Connect ID token verification (OAUTH_CLAIMS_SOURCE=id_token).

The ID token comes straight from the provider's token endpoint, but the
endpoint may be a plain-HTTP in-cluster address, so the token is checked in
full: signature against the provider's published keys, issuer, audience,
expiry and the nonce sent with the authorization request.
"""
from __future__ import annotations

import time

import httpx
import jwt

from app.core.config import Settings

# Asymmetric algorithms only: never "none", never HMAC (which would let anyone
# who knows the client secret mint tokens).
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
LEEWAY_SECONDS = 60
KEYS_TTL_SECONDS = 300
_REFETCH_MIN_SECONDS = 10

_jwks_url_cache: dict[str, str] = {}  # issuer -> discovered jwks_uri
_keys_cache: dict[str, tuple[float, dict]] = {}  # jwks URL -> (fetched at, {kid: PyJWK})


class IdTokenError(Exception):
    """The ID token is missing, malformed or fails verification."""


def reset_caches() -> None:
    """For tests."""
    _jwks_url_cache.clear()
    _keys_cache.clear()


async def _jwks_url(settings: Settings, http: httpx.AsyncClient) -> str:
    if settings.OAUTH_JWKS_URL:
        return settings.OAUTH_JWKS_URL
    issuer = settings.OAUTH_ISSUER.rstrip("/")
    url = _jwks_url_cache.get(issuer)
    if url:
        return url
    r = await http.get(f"{issuer}/.well-known/openid-configuration")
    r.raise_for_status()
    url = r.json().get("jwks_uri")
    if not url:
        raise IdTokenError("the provider's discovery document names no jwks_uri")
    _jwks_url_cache[issuer] = url
    return url


async def _signing_key(kid: str | None, settings: Settings, http: httpx.AsyncClient):
    url = await _jwks_url(settings, http)
    now = time.monotonic()
    cached = _keys_cache.get(url)
    # Refetch when the cache is old, or when the token names a key we don't
    # have (the provider rotated its keys), but not more than every few seconds.
    if cached is None or now - cached[0] > KEYS_TTL_SECONDS or (
        kid not in cached[1] and now - cached[0] > _REFETCH_MIN_SECONDS
    ):
        r = await http.get(url)
        r.raise_for_status()
        keys = {}
        for jwk in r.json().get("keys", []):
            if jwk.get("use", "sig") != "sig":
                continue
            try:
                keys[jwk.get("kid")] = jwt.PyJWK(jwk)
            except jwt.PyJWKError:
                continue  # an algorithm or key type we don't use
        cached = (now, keys)
        _keys_cache[url] = cached
    keys = cached[1]
    if kid is None and len(keys) == 1:
        return next(iter(keys.values()))
    if kid not in keys:
        raise IdTokenError("the ID token is signed with a key the provider doesn't publish")
    return keys[kid]


async def verify_id_token(
    id_token: str | None,
    *,
    settings: Settings,
    http: httpx.AsyncClient,
    nonce: str | None,
) -> dict:
    """The verified claims of <id_token>, or IdTokenError."""
    if not id_token:
        raise IdTokenError("no id_token in the token response (is the openid scope requested?)")
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as e:
        raise IdTokenError(f"malformed ID token: {e}") from e
    if header.get("alg") not in ALGORITHMS:
        raise IdTokenError(f"ID token algorithm {header.get('alg')!r} is not allowed")
    try:
        key = await _signing_key(header.get("kid"), settings, http)
    except httpx.HTTPError as e:
        raise IdTokenError(f"can't fetch the provider's signing keys: {e}") from e
    try:
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=ALGORITHMS,
            audience=settings.OAUTH_CLIENT_ID,
            issuer=settings.OAUTH_ISSUER.rstrip("/"),
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise IdTokenError(f"ID token rejected: {e}") from e
    # Several audiences: the token must have been issued to this client.
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != settings.OAUTH_CLIENT_ID:
        raise IdTokenError("ID token issued to another client (azp)")
    if nonce is not None and claims.get("nonce") != nonce:
        raise IdTokenError("ID token nonce doesn't match the sign-in request")
    return claims
