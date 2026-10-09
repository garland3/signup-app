"""OAUTH_CLAIMS_SOURCE=id_token and OAUTH_USER_ID_FIELD."""
import json
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient, Response

from app.core import oidc
from app.core.config import Settings
from app.core.middleware import AuthMiddleware
from app.core.rate_limit import limiter
from app.core.sessions import InMemorySessionMiddleware
from app.routes.auth import router as auth_router
from app.routes.keys import router as keys_router

ISSUER = "https://idp.example.com/realms/platform"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"
LITELLM = "http://mock-litellm:4000"
CLIENT_ID = "signup-app"
CLIENT_SECRET = "client-secret-0123456789abcdefghijklmnop"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY1, KEY2 = _key(), _key()


def _jwk(key, kid):
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


def _token(claims=None, *, key=KEY1, kid="k1", alg="RS256", **overrides):
    now = int(time.time())
    body = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "0b9f-sub",
        "iat": now,
        "exp": now + 300,
        "email": "alice@example.com",
        **(claims or {}),
    }
    body.update(overrides)
    return jwt.encode(body, key, algorithm=alg, headers={"kid": kid})


def _settings(**overrides) -> Settings:
    base = dict(
        AUTH_MODE="oauth",
        OAUTH_CLIENT_ID=CLIENT_ID,
        OAUTH_CLIENT_SECRET=CLIENT_SECRET,
        OAUTH_AUTHORIZE_URL=f"{ISSUER}/protocol/openid-connect/auth",
        OAUTH_TOKEN_URL=f"{ISSUER}/protocol/openid-connect/token",
        OAUTH_REDIRECT_URL="http://test/api/auth/callback",
        OAUTH_SCOPES="openid email",
        OAUTH_CLAIMS_SOURCE="id_token",
        OAUTH_ISSUER=ISSUER,
        OAUTH_USER_ID_FIELD="sub",
        SESSION_SECRET="test-secret-do-not-use-in-prod",
        LITELLM_BASE_URL=LITELLM,
        LITELLM_ADMIN_KEY="sk-test-admin-key",
        DEBUG_MODE=False,
    )
    base.update(overrides)
    return Settings(**base)


def _app(settings: Settings) -> FastAPI:
    import app.core.config as config_mod

    config_mod.settings = settings
    limiter.reset()
    app = FastAPI()
    app.add_middleware(AuthMiddleware, settings=settings)
    app.add_middleware(
        InMemorySessionMiddleware,
        cookie_name=settings.SESSION_COOKIE_NAME,
        max_age=settings.SESSION_MAX_AGE,
        idle_timeout=settings.SESSION_IDLE_TIMEOUT,
        https_only=False,
    )
    app.include_router(auth_router)
    app.include_router(keys_router)

    @app.get("/api/me")
    async def me(request: Request):
        return {"email": request.state.user_email, "user_id": request.state.user_id}

    return app


@pytest.fixture(autouse=True)
def _fresh_caches():
    oidc.reset_caches()
    yield
    oidc.reset_caches()


def _mock_provider(keys=None):
    respx.get(f"{ISSUER}/.well-known/openid-configuration").mock(
        return_value=Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URL})
    )
    return respx.get(JWKS_URL).mock(
        return_value=Response(200, json={"keys": keys or [_jwk(KEY1, "k1")]})
    )


async def _sign_in(c: AsyncClient, make_token):
    """Log in, answering the token request with make_token(nonce); the callback response."""
    login = await c.get("/api/auth/login", follow_redirects=False)
    q = parse_qs(urlparse(login.headers["location"]).query)
    nonce = q.get("nonce", [None])[0]
    respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
        return_value=Response(200, json={"access_token": "at", "id_token": make_token(nonce)})
    )
    return await c.get(
        f"/api/auth/callback?code=abc&state={q['state'][0]}", follow_redirects=False
    )


@pytest.mark.asyncio
async def test_login_sends_nonce_only_with_id_token_claims():
    async with AsyncClient(transport=ASGITransport(app=_app(_settings())), base_url="http://test") as c:
        r = await c.get("/api/auth/login", follow_redirects=False)
    assert parse_qs(urlparse(r.headers["location"]).query).get("nonce")

    plain = _settings(OAUTH_CLAIMS_SOURCE="userinfo", OAUTH_USERINFO_URL=f"{ISSUER}/userinfo")
    async with AsyncClient(transport=ASGITransport(app=_app(plain)), base_url="http://test") as c:
        r = await c.get("/api/auth/login", follow_redirects=False)
    assert "nonce" not in parse_qs(urlparse(r.headers["location"]).query)


@pytest.mark.asyncio
@respx.mock
async def test_callback_with_verified_id_token_sets_user_id_and_email():
    _mock_provider()
    async with AsyncClient(transport=ASGITransport(app=_app(_settings())), base_url="http://test") as c:
        cb = await _sign_in(c, lambda nonce: _token(nonce=nonce))
        assert cb.status_code == 302
        me = await c.get("/api/me")
    assert me.json() == {"email": "alice@example.com", "user_id": "0b9f-sub"}


@pytest.mark.asyncio
@respx.mock
async def test_without_user_id_field_the_email_is_the_user_id():
    _mock_provider()
    app = _app(_settings(OAUTH_USER_ID_FIELD=""))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await _sign_in(c, lambda nonce: _token(nonce=nonce))
        me = await c.get("/api/me")
    assert me.json() == {"email": "alice@example.com", "user_id": "alice@example.com"}


@pytest.mark.parametrize(
    "make_token",
    [
        pytest.param(lambda nonce: _token(nonce="someone-elses"), id="nonce mismatch"),
        pytest.param(lambda nonce: _token(), id="no nonce"),
        pytest.param(lambda nonce: _token(nonce=nonce, aud="other-client"), id="wrong audience"),
        pytest.param(lambda nonce: _token(nonce=nonce, iss="https://evil.example.com"), id="wrong issuer"),
        pytest.param(lambda nonce: _token(nonce=nonce, exp=int(time.time()) - 600), id="expired"),
        pytest.param(lambda nonce: _token(nonce=nonce, key=KEY2), id="signed with another key"),
        pytest.param(
            lambda nonce: _token(nonce=nonce, key=CLIENT_SECRET, alg="HS256"), id="HMAC with the client secret"
        ),
        pytest.param(
            lambda nonce: jwt.encode(
                {"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "nonce": nonce, "exp": int(time.time()) + 60,
                 "iat": int(time.time()), "email": "alice@example.com"},
                key=None, algorithm="none",
            ),
            id="alg none",
        ),
        pytest.param(
            lambda nonce: _token(nonce=nonce, aud=[CLIENT_ID, "other"], azp="other"), id="issued to another client"
        ),
        pytest.param(lambda nonce: "not-a-jwt", id="malformed"),
    ],
)
@pytest.mark.asyncio
@respx.mock
async def test_callback_rejects_bad_id_tokens(make_token):
    _mock_provider()
    async with AsyncClient(transport=ASGITransport(app=_app(_settings())), base_url="http://test") as c:
        cb = await _sign_in(c, make_token)
        assert cb.status_code == 502
        assert (await c.get("/api/me")).status_code == 401


@pytest.mark.asyncio
@respx.mock
async def test_callback_rejects_a_token_response_without_id_token():
    _mock_provider()
    async with AsyncClient(transport=ASGITransport(app=_app(_settings())), base_url="http://test") as c:
        login = await c.get("/api/auth/login", follow_redirects=False)
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
            return_value=Response(200, json={"access_token": "at"})
        )
        cb = await c.get(f"/api/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert cb.status_code == 502


@pytest.mark.asyncio
@respx.mock
async def test_callback_rejects_a_token_without_the_user_id_claim():
    _mock_provider()
    app = _app(_settings(OAUTH_USER_ID_FIELD="oid"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cb = await _sign_in(c, lambda nonce: _token(nonce=nonce))
    assert cb.status_code == 400
    assert "oid" in cb.json()["detail"]


@pytest.mark.asyncio
@respx.mock
async def test_jwks_url_setting_skips_discovery():
    discovery = respx.get(f"{ISSUER}/.well-known/openid-configuration").mock(
        return_value=Response(500)
    )
    internal = "http://keycloak.internal/realms/platform/protocol/openid-connect/certs"
    respx.get(internal).mock(return_value=Response(200, json={"keys": [_jwk(KEY1, "k1")]}))
    app = _app(_settings(OAUTH_JWKS_URL=internal))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cb = await _sign_in(c, lambda nonce: _token(nonce=nonce))
    assert cb.status_code == 302
    assert not discovery.called


@pytest.mark.asyncio
@respx.mock
async def test_rotated_signing_key_is_fetched():
    """A token signed with a key the cache doesn't have makes the app fetch the keys again."""
    import httpx

    jwks = _mock_provider(keys=[_jwk(KEY1, "k1"), _jwk(KEY2, "k2")])
    settings = _settings()
    async with httpx.AsyncClient() as http:
        oidc._keys_cache[JWKS_URL] = (time.monotonic() - 30, {"k1": jwt.PyJWK(_jwk(KEY1, "k1"))})
        claims = await oidc.verify_id_token(
            _token(key=KEY2, kid="k2", nonce="n"), settings=settings, http=http, nonce="n"
        )
    assert claims["sub"] == "0b9f-sub"
    assert jwks.called


@pytest.mark.asyncio
async def test_login_misconfigured_without_issuer():
    app = _app(_settings(OAUTH_ISSUER=""))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/auth/login", follow_redirects=False)
    assert r.status_code == 500
    assert "OAUTH_ISSUER" in r.json()["detail"]


@pytest.mark.asyncio
@respx.mock
async def test_routes_use_the_user_id_and_new_users_get_the_email():
    _mock_provider()
    respx.get(f"{LITELLM}/user/info").mock(return_value=Response(404, json={}))
    new_user = respx.post(f"{LITELLM}/user/new").mock(return_value=Response(200, json={}))
    generate = respx.post(f"{LITELLM}/key/generate").mock(
        return_value=Response(200, json={"key": "sk-new", "token_id": "t1", "user_id": "0b9f-sub"})
    )
    listing = respx.get(f"{LITELLM}/key/list").mock(return_value=Response(200, json={"keys": []}))
    async with AsyncClient(transport=ASGITransport(app=_app(_settings())), base_url="http://test") as c:
        await _sign_in(c, lambda nonce: _token(nonce=nonce))
        r = await c.post("/api/keys", json={"name": "k"}, headers={"Origin": "http://test"})
        assert r.status_code == 201
        assert (await c.get("/api/keys")).status_code == 200
    user = json.loads(new_user.calls[0].request.content)
    assert user["user_id"] == "0b9f-sub" and user["user_email"] == "alice@example.com"
    key = json.loads(generate.calls[0].request.content)
    assert key["user_id"] == "0b9f-sub"
    assert key["key_alias"].startswith("alice@example.com-")  # aliases stay readable
    assert listing.calls[0].request.url.params["user_id"] == "0b9f-sub"


@pytest.mark.asyncio
@respx.mock
async def test_someone_elses_key_is_not_found_by_user_id():
    """Ownership is by user ID: a key of the same email under another ID isn't yours."""
    _mock_provider()
    respx.get(f"{LITELLM}/key/info").mock(
        return_value=Response(200, json={"info": {"user_id": "alice@example.com"}})
    )
    async with AsyncClient(transport=ASGITransport(app=_app(_settings())), base_url="http://test") as c:
        await _sign_in(c, lambda nonce: _token(nonce=nonce))
        r = await c.delete("/api/keys/tok1", headers={"Origin": "http://test"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_ensure_user_without_a_separate_id_keeps_the_old_shape():
    from app.core.litellm_client import LiteLLMClient

    with respx.mock:
        respx.get(f"{LITELLM}/user/info").mock(return_value=Response(404, json={}))
        new_user = respx.post(f"{LITELLM}/user/new").mock(return_value=Response(200, json={}))
        await LiteLLMClient(_settings()).ensure_user("alice@example.com", email="alice@example.com")
    assert "user_email" not in json.loads(new_user.calls[0].request.content)


def test_startup_refuses_id_token_claims_without_issuer():
    from app.main import _enforce_startup_safety

    with pytest.raises(RuntimeError, match="OAUTH_ISSUER"):
        _enforce_startup_safety(_settings(OAUTH_ISSUER=""))


@pytest.mark.asyncio
async def test_ensure_user_with_a_taken_email_creates_the_user_without_it():
    """LiteLLM keeps emails unique: a recreated account (new ID, same email) still gets a user."""
    from app.core.litellm_client import LiteLLMClient

    with respx.mock:
        respx.get(f"{LITELLM}/user/info").mock(return_value=Response(404, json={}))
        new_user = respx.post(f"{LITELLM}/user/new").mock(
            side_effect=[
                Response(409, json={"error": {"message": "User with email alice@example.com already exists"}}),
                Response(200, json={}),
            ]
        )
        await LiteLLMClient(_settings()).ensure_user("new-sub", email="alice@example.com")
    first, second = (json.loads(c.request.content) for c in new_user.calls)
    assert first["user_email"] == "alice@example.com"
    assert second["user_id"] == "new-sub" and "user_email" not in second


@pytest.mark.asyncio
async def test_ensure_user_other_errors_still_fail():
    import httpx

    from app.core.litellm_client import LiteLLMClient

    with respx.mock:
        respx.get(f"{LITELLM}/user/info").mock(return_value=Response(404, json={}))
        respx.post(f"{LITELLM}/user/new").mock(return_value=Response(500, json={"error": "boom"}))
        with pytest.raises(httpx.HTTPStatusError):
            await LiteLLMClient(_settings()).ensure_user("new-sub", email="alice@example.com")
