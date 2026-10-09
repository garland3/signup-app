import logging
import secrets
from urllib.parse import unquote, urlencode, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.core.audit import audit
from app.core.config import Settings, get_settings
from app.core.oidc import IdTokenError, verify_id_token
from app.core.team_claims import groups_from_claims

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth")


def _is_safe_redirect(url: str) -> bool:
    """Return True only for safe, relative redirect paths."""
    # Decode percent-encoding to prevent %2F%2F bypasses
    decoded = unquote(url)
    # Must start with exactly one slash (reject // and \)
    if not decoded.startswith("/") or decoded.startswith("//") or decoded.startswith("/\\"):
        return False
    parsed = urlparse(decoded)
    return not parsed.netloc and not parsed.scheme


def _require_oauth_configured():
    s = get_settings()
    if s.AUTH_MODE != "oauth":
        raise HTTPException(status_code=404, detail="OAuth auth is not enabled")
    missing = [
        name
        for name, val in [
            ("OAUTH_CLIENT_ID", s.OAUTH_CLIENT_ID),
            ("OAUTH_CLIENT_SECRET", s.OAUTH_CLIENT_SECRET),
            ("OAUTH_AUTHORIZE_URL", s.OAUTH_AUTHORIZE_URL),
            ("OAUTH_TOKEN_URL", s.OAUTH_TOKEN_URL),
            ("OAUTH_REDIRECT_URL", s.OAUTH_REDIRECT_URL),
            ("SESSION_SECRET", s.SESSION_SECRET),
        ]
        + (
            [("OAUTH_ISSUER", s.OAUTH_ISSUER)]
            if s.OAUTH_CLAIMS_SOURCE == "id_token"
            else [("OAUTH_USERINFO_URL", s.OAUTH_USERINFO_URL)]
        )
        if not val
    ]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"OAuth misconfigured; missing: {', '.join(missing)}",
        )
    return s


@router.get("/login")
async def login(request: Request):
    s = _require_oauth_configured()
    default_next = s.normalized_root_path + "/"
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    # Optional post-login redirect target (validated to prevent open redirects)
    next_url = request.query_params.get("next", default_next)
    if not _is_safe_redirect(next_url):
        next_url = default_next
    request.session["oauth_next"] = next_url

    params = {
        "response_type": "code",
        "client_id": s.OAUTH_CLIENT_ID,
        "redirect_uri": s.OAUTH_REDIRECT_URL,
        "scope": s.OAUTH_SCOPES,
        "state": state,
    }
    if s.OAUTH_CLAIMS_SOURCE == "id_token":
        # Binds the ID token to this sign-in (checked in the callback).
        nonce = secrets.token_urlsafe(32)
        request.session["oauth_nonce"] = nonce
        params["nonce"] = nonce
    return RedirectResponse(
        f"{s.OAUTH_AUTHORIZE_URL}?{urlencode(params)}", status_code=302
    )


@router.get("/callback")
async def callback(request: Request):
    s = _require_oauth_configured()

    error = request.query_params.get("error")
    if error:
        audit("oauth_callback_error", error=error)
        raise HTTPException(
            status_code=400,
            detail=f"OAuth error: {error} - "
            f"{request.query_params.get('error_description', '')}",
        )

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or not secrets.compare_digest(state, expected_state):
        audit("oauth_state_mismatch")
        raise HTTPException(status_code=400, detail="Invalid state")
    nonce = request.session.pop("oauth_nonce", None)

    # Exchange code for access token
    async with httpx.AsyncClient(timeout=10.0) as http:
        try:
            token_resp = await http.post(
                s.OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": s.OAUTH_REDIRECT_URL,
                    "client_id": s.OAUTH_CLIENT_ID,
                    "client_secret": s.OAUTH_CLIENT_SECRET,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.RequestError as e:
            logger.exception("Token exchange transport error")
            audit("oauth_token_exchange_error", reason="transport")
            raise HTTPException(status_code=502, detail="Token exchange failed")
        if token_resp.status_code >= 400:
            logger.error("Token exchange failed: %s", token_resp.text)
            audit("oauth_token_exchange_error", status=token_resp.status_code)
            raise HTTPException(status_code=502, detail="Token exchange failed")
        tokens = token_resp.json()
        # The nonce must match (a sign-in without one in the session fails).
        claims = await claims_from_tokens(tokens, s, http, nonce or "")

    email = claims.get(s.OAUTH_EMAIL_FIELD)
    if not email or not isinstance(email, str):
        raise HTTPException(
            status_code=400,
            detail=f"Email not found in {_source(s)} (field: {s.OAUTH_EMAIL_FIELD})",
        )
    # The LiteLLM user ID: a stable claim such as sub or oid, or the email.
    user_id = email
    if s.OAUTH_USER_ID_FIELD:
        user_id = claims.get(s.OAUTH_USER_ID_FIELD)
        if not user_id or not isinstance(user_id, str):
            raise HTTPException(
                status_code=400,
                detail=f"User ID not found in {_source(s)} (field: {s.OAUTH_USER_ID_FIELD})",
            )
        request.session["user_id"] = user_id

    request.session["user_email"] = email
    if s.FEATURE_TEAMS_ENABLED and s.TEAM_SOURCE == "claims":
        # Teams come from the identity provider: keep the groups, and the
        # refresh token to re-read them before a key is made.
        request.session["groups"] = groups_from_claims(claims, s)
        if tokens.get("refresh_token"):
            request.session["refresh_token"] = tokens["refresh_token"]
    audit("login_success", user=email, user_id=user_id)
    default_next = s.normalized_root_path + "/"
    next_url = request.session.pop("oauth_next", default_next) or default_next
    if not _is_safe_redirect(next_url):
        next_url = default_next
    return RedirectResponse(next_url, status_code=302)


def _source(s: Settings) -> str:
    return "the ID token" if s.OAUTH_CLAIMS_SOURCE == "id_token" else "userinfo"


async def claims_from_tokens(
    tokens: dict, s: Settings, http: httpx.AsyncClient, nonce: str | None
) -> dict:
    """The user's claims from a token response: the verified ID token's, or
    the userinfo endpoint's (called with the access token)."""
    if s.OAUTH_CLAIMS_SOURCE == "id_token":
        try:
            return await verify_id_token(
                tokens.get("id_token"), settings=s, http=http, nonce=nonce
            )
        except IdTokenError as e:
            logger.error("ID token verification failed: %s", e)
            audit("oauth_id_token_error", reason=str(e))
            raise HTTPException(status_code=502, detail="Sign-in could not be verified")

    access_token = tokens.get("access_token")
    if not access_token:
        audit("oauth_token_exchange_error", reason="no_access_token")
        raise HTTPException(
            status_code=502, detail="No access_token in token response"
        )
    try:
        ui_resp = await http.get(
            s.OAUTH_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except httpx.RequestError:
        logger.exception("Userinfo fetch transport error")
        audit("oauth_userinfo_error", reason="transport")
        raise HTTPException(status_code=502, detail="Userinfo fetch failed")
    if ui_resp.status_code >= 400:
        logger.error("Userinfo fetch failed: %s", ui_resp.text)
        audit("oauth_userinfo_error", status=ui_resp.status_code)
        raise HTTPException(status_code=502, detail="Userinfo fetch failed")
    return ui_resp.json()


@router.post("/logout")
async def logout(request: Request):
    user = None
    session = getattr(request, "session", None)
    if session is not None:
        user = session.get("user_email")
        session.clear()
    audit("logout", user=user)
    return RedirectResponse(
        get_settings().normalized_root_path + "/", status_code=303
    )
