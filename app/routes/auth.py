import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.core.config import get_settings

router = APIRouter(prefix="/api/auth")


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
            ("OAUTH_USERINFO_URL", s.OAUTH_USERINFO_URL),
            ("OAUTH_REDIRECT_URL", s.OAUTH_REDIRECT_URL),
            ("SESSION_SECRET", s.SESSION_SECRET),
        ]
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
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    # Optional post-login redirect target
    next_url = request.query_params.get("next", "/")
    request.session["oauth_next"] = next_url

    params = {
        "response_type": "code",
        "client_id": s.OAUTH_CLIENT_ID,
        "redirect_uri": s.OAUTH_REDIRECT_URL,
        "scope": s.OAUTH_SCOPES,
        "state": state,
    }
    return RedirectResponse(
        f"{s.OAUTH_AUTHORIZE_URL}?{urlencode(params)}", status_code=302
    )


@router.get("/callback")
async def callback(request: Request):
    s = _require_oauth_configured()

    error = request.query_params.get("error")
    if error:
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
        raise HTTPException(status_code=400, detail="Invalid state")

    # Exchange code for access token
    async with httpx.AsyncClient(timeout=10.0) as http:
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
        if token_resp.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Token exchange failed: {token_resp.text}",
            )
        tokens = token_resp.json()
        access_token = tokens.get("access_token")
        if not access_token:
            raise HTTPException(
                status_code=502, detail="No access_token in token response"
            )

        # Fetch userinfo
        ui_resp = await http.get(
            s.OAUTH_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if ui_resp.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Userinfo fetch failed: {ui_resp.text}",
            )
        userinfo = ui_resp.json()

    email = userinfo.get(s.OAUTH_EMAIL_FIELD)
    if not email:
        raise HTTPException(
            status_code=400,
            detail=f"Email not found in userinfo (field: {s.OAUTH_EMAIL_FIELD})",
        )

    request.session["user_email"] = email
    next_url = request.session.pop("oauth_next", "/") or "/"
    # Only allow relative redirects
    if not next_url.startswith("/"):
        next_url = "/"
    return RedirectResponse(next_url, status_code=302)


@router.post("/logout")
@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=302)
