"""Log out ends the identity provider's session too (OpenID Connect RP-initiated logout)."""
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from tests.test_oidc import ISSUER, JWKS_URL, KEY1, _app, _jwk, _settings, _sign_in, _token

END_SESSION = f"{ISSUER}/protocol/openid-connect/logout"


@pytest.fixture(autouse=True)
def _fresh_discovery():
    """Each test starts without a cached discovery document."""
    from app.core import oidc

    oidc.reset_caches()
    yield
    oidc.reset_caches()


def _provider(**discovery):
    respx.get(f"{ISSUER}/.well-known/openid-configuration").mock(
        return_value=Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URL, **discovery})
    )
    respx.get(JWKS_URL).mock(return_value=Response(200, json={"keys": [_jwk(KEY1, "k1")]}))


async def _sign_in_and_out(settings):
    """Sign in, then log out: (the ID token, the logout response, /api/me's status after)."""
    tokens = []

    def make(nonce):
        tokens.append(_token(nonce=nonce))
        return tokens[-1]

    async with AsyncClient(transport=ASGITransport(app=_app(settings)), base_url="http://test") as c:
        await _sign_in(c, make)
        assert (await c.get("/api/me")).status_code == 200
        r = await c.post("/api/auth/logout", follow_redirects=False)
        after = (await c.get("/api/me")).status_code
    return tokens[0], r, after


@pytest.mark.asyncio
@respx.mock
async def test_log_out_goes_to_the_providers_logout_from_discovery():
    _provider(end_session_endpoint=END_SESSION)
    id_token, r, after = await _sign_in_and_out(_settings())
    assert r.status_code == 303 and after == 401
    url = urlparse(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == END_SESSION
    assert parse_qs(url.query) == {
        "client_id": ["signup-app"],
        "post_logout_redirect_uri": ["http://test/"],
        # The provider ends the session without asking the person to confirm.
        "id_token_hint": [id_token],
    }


@pytest.mark.asyncio
@respx.mock
async def test_oauth_logout_url_wins_over_discovery():
    _provider(end_session_endpoint=END_SESSION)
    _, r, _ = await _sign_in_and_out(_settings(
        OAUTH_LOGOUT_URL="https://login.example.com/logout?tenant=t",
        OAUTH_POST_LOGOUT_REDIRECT_URL="https://keys.example.com/signed-out",
    ))
    url = urlparse(r.headers["location"])
    assert url.netloc == "login.example.com" and url.path == "/logout"
    q = parse_qs(url.query)
    assert q["tenant"] == ["t"] and q["post_logout_redirect_uri"] == ["https://keys.example.com/signed-out"]


@pytest.mark.parametrize("discovery", [
    Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URL}),  # no end_session_endpoint
    Response(503),  # discovery fails
    Response(200, json=["not", "an", "object"]),
    # A logout on another host than the issuer: the page's CSP form-action allows only
    # the issuer's, so the browser would block the redirect (set OAUTH_LOGOUT_URL).
    Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URL,
                        "end_session_endpoint": "https://auth.example.net/logout"}),
])
@pytest.mark.asyncio
async def test_without_a_logout_endpoint_only_the_apps_session_ends(discovery):
    with respx.mock:
        _provider()
        async with AsyncClient(transport=ASGITransport(app=_app(_settings())), base_url="http://test") as c:
            await _sign_in(c, lambda nonce: _token(nonce=nonce))
            from app.core import oidc

            oidc.reset_caches()  # the sign-in cached the working discovery document
            respx.get(f"{ISSUER}/.well-known/openid-configuration").mock(return_value=discovery)
            r = await c.post("/api/auth/logout", follow_redirects=False)
            assert (await c.get("/api/me")).status_code == 401
    assert r.status_code == 303 and r.headers["location"] == "/"


@pytest.mark.parametrize("overrides, url", [
    ({}, "http://test/"),
    ({"ROOT_PATH": "/keys", "OAUTH_REDIRECT_URL": "https://h.example/keys/api/auth/callback"},
     "https://h.example/keys/"),
    ({"OAUTH_POST_LOGOUT_REDIRECT_URL": "https://h.example/bye"}, "https://h.example/bye"),
    ({"OAUTH_REDIRECT_URL": ""}, ""),
])
def test_post_logout_redirect_url(overrides, url):
    assert _settings(**overrides).post_logout_redirect_url == url


@pytest.mark.parametrize("overrides, origin", [
    ({}, "https://idp.example.com"),  # the issuer's: discovery's end_session_endpoint
    ({"OAUTH_LOGOUT_URL": "https://login.example.com/t/logout?x=1"}, "https://login.example.com"),
    ({"AUTH_MODE": "proxy"}, ""),
])
def test_logout_form_origin(overrides, origin):
    assert _settings(**overrides).logout_form_origin == origin


@pytest.mark.parametrize("overrides, form_action", [
    ({"AUTH_MODE": "oauth", "OAUTH_ISSUER": ISSUER, "SESSION_SECRET": "s"}, "'self' https://idp.example.com"),
    ({}, "'self'"),
])
@pytest.mark.asyncio
async def test_the_page_lets_log_out_redirect_to_the_provider(overrides, form_action):
    """Browsers apply CSP form-action to a form's redirects: without the provider's
    origin there, they block Log out's redirect to it and stay on the page."""
    from tests.test_security_headers import _make_app

    app = _make_app(**overrides)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/health")
    assert f"form-action {form_action};" in r.headers["content-security-policy"]
