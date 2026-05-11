from __future__ import annotations

import html
import json
import logging
import os
import secrets
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from joserfc import jwk, jwt
from joserfc.errors import ClaimError, ExpiredTokenError, InvalidClaimError, JoseError
from joserfc.jwk import ECKey
from joserfc.jwt import JWTClaimsRegistry
from starlette.middleware.sessions import SessionMiddleware

from config import (
    COOKIE_NAME,
    COOKIE_SAMESITE,
    COOKIE_SECURE,
    ENABLE_GITHUB,
    ENABLE_GOOGLE,
    ENABLE_MICROSOFT,
    EXTERNAL_BASE,
    GITHUB_ALLOWED_ORGS,
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    GOOGLE_ALLOWED_DOMAINS,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    JWT_ALG,
    JWT_AUD,
    JWT_CLOCK_SKEW_SECONDS,
    JWT_ISS,
    JWT_KID,
    JWT_PRIVATE_KEY_PATH,
    JWT_PRIVATE_KEY_PEM,
    JWT_TTL_SECONDS,
    MICROSOFT_ALLOWED_DOMAINS,
    MICROSOFT_ALLOWED_TENANT_IDS,
    MS_CLIENT_ID,
    MS_CLIENT_SECRET,
    MS_TENANT_ID,
    SESSION_SECRET,
    USE_UVLOOP,
    enabled_providers_effective,
    get_redirect,
    has_jwt_signing_material,
)
from frontend_logger import log
from metrics import record_auth_event
from rate_limits import limiter, limits, _auth_ip_key

if JWT_ALG != "ES256":
    raise RuntimeError(f"Unsupported JWT_ALG={JWT_ALG!r}. This service requires ES256.")

if not SESSION_SECRET:
    raise RuntimeError("SESSION_SECRET is required for the OIDC login flow.")

if not has_jwt_signing_material():
    raise RuntimeError("JWT signing material is required. Set JWT_PRIVATE_KEY_PEM or JWT_PRIVATE_KEY_PATH.")

if not JWT_KID:
    raise RuntimeError("JWT_KID is required for JWKS stability.")

app = FastAPI(title="stateless-openid-auth", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie=COOKIE_NAME,
    same_site=COOKIE_SAMESITE,
    https_only=COOKIE_SECURE,
)

# Provider SVG icons
_GOOGLE_SVG = '<svg viewBox="0 0 24 24" width="18" height="18" xmlns="http://www.w3.org/2000/svg"><path fill="#EA4335" d="M12 10.2v3.6h5.2c-.2 1.2-1.4 3.6-5.2 3.6-3.1 0-5.6-2.6-5.6-5.8S8.9 6.8 12 6.8c1.8 0 2.9.8 3.6 1.5l2.4-2.3C17.2 4 14.8 3 12 3 7.6 3 4 6.6 4 11s3.6 8 8 8c4.6 0 7-3.2 7-7.7 0-.5 0-.9-.1-1.1H12z"/></svg>'
_MICROSOFT_SVG = '<svg viewBox="0 0 24 24" width="18" height="18" xmlns="http://www.w3.org/2000/svg"><rect x="2" y="2" width="9" height="9" fill="#F35325"/><rect x="13" y="2" width="9" height="9" fill="#81BC06"/><rect x="2" y="13" width="9" height="9" fill="#05A6F0"/><rect x="13" y="13" width="9" height="9" fill="#FFBA08"/></svg>'
_GITHUB_SVG = '<svg viewBox="0 0 24 24" width="18" height="18" xmlns="http://www.w3.org/2000/svg"><path fill="#111" d="M12 .5C5.6.5.5 5.6.5 12c0 5.1 3.3 9.4 7.9 10.9.6.1.8-.3.8-.6v-2.2c-3.2.7-3.9-1.4-3.9-1.4-.5-1.1-1.2-1.4-1.2-1.4-1-.7.1-.7.1-.7 1.1.1 1.7 1.1 1.7 1.1 1 .1 1.6.8 2 .6.1-.8.4-1.4.7-1.8-2.6-.3-5.4-1.3-5.4-5.8 0-1.3.5-2.4 1.3-3.2-.1-.3-.6-1.6.1-3.3 0 0 1-.3 3.3 1.3.9-.3 1.9-.5 2.9-.5s2 .2 2.9.5c2.3-1.6 3.3-1.3 3.3-1.3.7 1.7.2 3 .1 3.3.8.8 1.3 1.9 1.3 3.2 0 4.5-2.8 5.5-5.5 5.8.5.4.8 1.1.8 2.3v3.4c0 .3.2.8.8.6 4.6-1.5 7.9-5.8 7.9-10.9C23.5 5.6 18.4.5 12 .5z"/></svg>'


def _load_private_key():
    raw = (JWT_PRIVATE_KEY_PEM or "").strip()
    if not raw and JWT_PRIVATE_KEY_PATH:
        path = Path(JWT_PRIVATE_KEY_PATH)
        raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise RuntimeError("JWT signing key is missing.")
    if raw.lstrip().startswith("{"):
        return jwk.import_key(json.loads(raw))
    return ECKey.import_key(raw)


SIGNING_KEY = _load_private_key()
PUBLIC_KEY = ECKey.import_key(SIGNING_KEY.as_dict(private=False))
PUBLIC_JWK = PUBLIC_KEY.as_dict(private=False)
PUBLIC_JWK["kid"] = JWT_KID
PUBLIC_JWKS = {"keys": [PUBLIC_JWK]}


class _SkewClaimsRegistry(JWTClaimsRegistry):
    def validate_exp(self, value):
        if not isinstance(value, (int, float)):
            raise InvalidClaimError("exp")
        if _jwt_now().timestamp() > float(value) + JWT_CLOCK_SKEW_SECONDS:
            raise ExpiredTokenError("exp")

    def validate_iat(self, value):
        if not isinstance(value, (int, float)):
            raise InvalidClaimError("iat")
        if _jwt_now().timestamp() + JWT_CLOCK_SKEW_SECONDS < float(value):
            raise InvalidClaimError("iat")

    def validate_nbf(self, value):
        if not isinstance(value, (int, float)):
            raise InvalidClaimError("nbf")
        if _jwt_now().timestamp() + JWT_CLOCK_SKEW_SECONDS < float(value):
            raise InvalidClaimError("nbf")


CLAIMS_REQUESTS = _SkewClaimsRegistry(
    iss={"essential": True, "value": JWT_ISS},
    aud={"essential": True, "value": JWT_AUD},
    sub={"essential": True},
    exp={"essential": True},
    iat={"essential": True},
)

oauth = OAuth()
ENABLED_PROVIDERS = enabled_providers_effective()
_provider_clients: dict[str, Any] = {}

if ENABLE_GOOGLE and "google" in ENABLED_PROVIDERS:
    try:
        oauth.register(
            name="google",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
        )
        log.info("Google auth provider registered")
    except Exception as e:
        log.error("Failed to register Google auth", error=str(e))
        ENABLED_PROVIDERS.remove("google")

if ENABLE_MICROSOFT and "microsoft" in ENABLED_PROVIDERS:
    try:
        tenant = MS_TENANT_ID or "common"
        oauth.register(
            name="microsoft",
            client_id=MS_CLIENT_ID,
            client_secret=MS_CLIENT_SECRET,
            server_metadata_url=f"https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile offline_access User.Read", "code_challenge_method": "S256"},
        )
        log.info("Microsoft auth provider registered")
    except Exception as e:
        log.error("Failed to register Microsoft auth", error=str(e))
        ENABLED_PROVIDERS.remove("microsoft")

if ENABLE_GITHUB and "github" in ENABLED_PROVIDERS:
    try:
        oauth.register(
            name="github",
            client_id=GITHUB_CLIENT_ID,
            client_secret=GITHUB_CLIENT_SECRET,
            access_token_url="https://github.com/login/oauth/access_token",
            authorize_url="https://github.com/login/oauth/authorize",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "user:email read:org", "code_challenge_method": "S256"},
        )
        log.info("GitHub auth provider registered")
    except Exception as e:
        log.error("Failed to register GitHub auth", error=str(e))
        ENABLED_PROVIDERS.remove("github")

if not ENABLED_PROVIDERS:
    log.warn("No OAuth providers are enabled. Login will not be possible.")


def _enabled_providers() -> list[str]:
    return ENABLED_PROVIDERS


def _frontend_base() -> str:
    return EXTERNAL_BASE.rstrip("/")


def _redirect_uri(provider: str) -> str:
    return get_redirect(provider)


def _provider_client(provider: str):
    if provider not in _provider_clients:
        client = oauth.create_client(provider)
        if client is None:
            raise HTTPException(status_code=500, detail=f"OAuth client unavailable for '{provider}'")
        _provider_clients[provider] = client
    return _provider_clients[provider]


def _session(request: Request):
    if getattr(request, "session", None) is None:
        raise HTTPException(status_code=500, detail="SessionMiddleware is required")
    return request.session


def _identity_from_userinfo(provider: str, userinfo: dict[str, Any]) -> dict[str, Any]:
    sub = userinfo.get("sub") or userinfo.get("id") or userinfo.get("node_id") or userinfo.get("oid")
    email = userinfo.get("email") or userinfo.get("mail") or userinfo.get("userPrincipalName")
    name = userinfo.get("name") or userinfo.get("displayName") or userinfo.get("login") or userinfo.get("preferred_username")
    tenant = userinfo.get("tid") or userinfo.get("tenantId")
    if isinstance(tenant, str):
        tenant = tenant.strip().lower()
    else:
        tenant = None
    return {
        "provider": provider,
        "sub": str(sub) if sub is not None else None,
        "email": email,
        "name": name,
        "tenant": tenant,
        "raw": userinfo,
    }


def _safe_email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[-1].strip().lower()


def _jwt_now() -> datetime:
    return datetime.now(UTC)


def mint_access_token(identity: dict[str, Any]) -> str:
    now = _jwt_now()
    claims = {
        "iss": JWT_ISS,
        "aud": JWT_AUD,
        "sub": str(identity["sub"]),
        "provider": identity["provider"],
        "email": identity.get("email"),
        "name": identity.get("name"),
        "iat": now,
        "exp": now + timedelta(seconds=JWT_TTL_SECONDS),
        "jti": secrets.token_urlsafe(16),
    }
    header = {"alg": JWT_ALG, "kid": JWT_KID}
    return jwt.encode(header, claims, SIGNING_KEY, algorithms=[JWT_ALG])


def verify_access_token(token_text: str) -> dict[str, Any]:
    token = jwt.decode(token_text, PUBLIC_KEY, algorithms=[JWT_ALG])
    claims = dict(token.claims)
    CLAIMS_REQUESTS.validate(claims)
    return claims


def _extract_bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = auth.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return token


def _render_access_denied(title: str, message: str, details: str | None = None, allowed: str | None = None) -> HTMLResponse:
    allowed_html = f"<p>Allowed: <code>{html.escape(allowed)}</code></p>" if allowed else ""
    details_html = f"<div style='margin-top:8px;font-size:90%;color:#666'>{html.escape(details)}</div>" if details else ""
    safe_front = _frontend_base()
    body = (
        "<!doctype html><html><head><meta charset='utf-8'><title>Access denied</title></head><body>"
        f"<div style='font-family:system-ui;margin:32px'>"
        f"<h2>{html.escape(title)}</h2>"
        f"<p>{html.escape(message)}</p>"
        f"{allowed_html}"
        f"{details_html}"
        f"<p><a href='{html.escape(safe_front + '/')}'>Return to application</a></p>"
        "</div></body></html>"
    )
    return HTMLResponse(content=body, status_code=403)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/redirects", response_class=HTMLResponse)
async def redirects_page():
    providers = _enabled_providers()
    rows = "".join(
        f"<li><strong>{html.escape(p)}</strong>: <code>{html.escape(_redirect_uri(p))}</code></li>"
        for p in providers
    ) or "<li>No providers enabled</li>"
    body = (
        "<!doctype html><html><head><meta charset='utf-8'><title>Redirect URIs</title></head><body>"
        f"<h2>Redirect URIs to register</h2><ul>{rows}</ul></body></html>"
    )
    return HTMLResponse(body)


@app.get("/login", response_class=HTMLResponse)
@limiter.limit(limits.auth_login, key_func=_auth_ip_key)
async def login_page(request: Request):
    providers = _enabled_providers()
    icons = {"google": _GOOGLE_SVG, "microsoft": _MICROSOFT_SVG, "github": _GITHUB_SVG}
    if not providers:
        btns_html = "<p>No login providers configured. Contact administrator.</p>"
    else:
        buttons = []
        for p in providers:
            buttons.append(
                f"<a href='/auth/login/start/{html.escape(p)}' "
                f"class='w-full inline-flex items-center justify-center border rounded py-2 px-3 mb-3' "
                f"aria-label='Continue with {p.capitalize()}'>"
                f"{icons.get(p, '')} <span style='margin-left:8px'>Continue with {p.capitalize()}</span></a>"
            )
        btns_html = "\n".join(buttons)

    expected_hint = "/auth/callback/<provider>"
    body = (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<link href='https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css' rel='stylesheet'>"
        "<title>Sign in</title></head><body class='bg-gray-50 min-h-screen flex items-center justify-center'>"
        "<div class='max-w-md w-full p-6'><div class='bg-white p-6 rounded shadow'>"
        "<h1 class='text-xl font-semibold mb-3'>Sign in</h1>"
        f"{btns_html}"
        f"<div class='mt-6 text-xs text-gray-400'>Expected redirect URIs follow pattern: <code>{expected_hint}</code></div>"
        "</div></div></body></html>"
    )
    return HTMLResponse(body)


@app.get("/login/start/{provider}")
@limiter.limit(limits.auth_start, key_func=_auth_ip_key)
async def login_start(request: Request, provider: str):
    provider = provider.strip().lower()
    if provider not in _enabled_providers():
        raise HTTPException(status_code=404, detail="Provider not enabled")
    client = _provider_client(provider)
    redirect_uri = _redirect_uri(provider)
    sess = _session(request)
    sess["oauth_provider"] = provider
    sess["oauth_started_at"] = int(_jwt_now().timestamp())
    state = secrets.token_urlsafe(32)
    sess["oauth_state"] = state
    try:
        return await client.authorize_redirect(request, redirect_uri, state=state)
    except OAuthError as exc:
        log.warn("oauth redirect failed", provider=provider, error=str(exc))
        raise HTTPException(status_code=502, detail="OAuth redirect initiation failed")
    except Exception as exc:
        log.error("oauth redirect failed", provider=provider, stack=traceback.format_exc())
        raise HTTPException(status_code=502, detail="OAuth redirect initiation failed")


async def _fetch_userinfo(provider: str, client, token: dict[str, Any]) -> dict[str, Any]:
    if isinstance(token.get("userinfo"), dict):
        return token["userinfo"]
    if provider in {"google", "microsoft"}:
        return {}
    if provider == "github":
        resp = await client.get("user", token=token)
        resp.raise_for_status()
        data = resp.json()
        email = data.get("email")
        if not email:
            resp2 = await client.get("user/emails", token=token)
            if resp2.status_code == 200:
                emails = resp2.json()
                if isinstance(emails, list):
                    primary = next((e["email"] for e in emails if e.get("primary") and e.get("verified")), None)
                    verified = next((e["email"] for e in emails if e.get("verified") and not primary), None)
                    email = primary or verified
        return {"id": data.get("id"), "node_id": data.get("node_id"), "login": data.get("login"), "name": data.get("name"), "email": email}
    return {}


def _deny_google(identity: dict[str, Any]) -> None:
    if not GOOGLE_ALLOWED_DOMAINS:
        return
    domain = _safe_email_domain(identity.get("email"))
    if not domain or domain not in GOOGLE_ALLOWED_DOMAINS:
        raise HTTPException(status_code=403, detail="Access denied")


def _deny_microsoft(identity: dict[str, Any]) -> None:
    if MICROSOFT_ALLOWED_TENANT_IDS:
        tenant = identity.get("tenant")
        if not tenant or tenant.lower() not in MICROSOFT_ALLOWED_TENANT_IDS:
            raise HTTPException(status_code=403, detail="Access denied")
    if MICROSOFT_ALLOWED_DOMAINS:
        domain = _safe_email_domain(identity.get("email"))
        if not domain or domain not in MICROSOFT_ALLOWED_DOMAINS:
            raise HTTPException(status_code=403, detail="Access denied")


def _deny_github(identity: dict[str, Any], orgs: list[str]) -> None:
    if GITHUB_ALLOWED_ORGS and not any(org in GITHUB_ALLOWED_ORGS for org in orgs):
        raise HTTPException(status_code=403, detail="Access denied")


@app.get("/callback/{provider}")
@limiter.limit(limits.auth_callback, key_func=_auth_ip_key)
async def callback(request: Request, provider: str):
    provider = provider.strip().lower()
    if provider not in _enabled_providers():
        raise HTTPException(status_code=404, detail="Provider not enabled")
    client = _provider_client(provider)
    sess = _session(request)
    stored_state = sess.pop("oauth_state", None)
    request_state = request.query_params.get("state")
    if not stored_state or stored_state != request_state:
        log.warn("oauth state mismatch", provider=provider)
        return RedirectResponse(url=f"{_frontend_base()}/?error=state_mismatch", status_code=302)

    # Attempt token exchange – relax issuer check for Microsoft
    token = None
    try:
        if provider == "microsoft":
            # Microsoft ID tokens have a different issuer, skip our strict check
            token = await client.authorize_access_token(request, claims_options=None)
        else:
            token = await client.authorize_access_token(request)
    except OAuthError as e:
        log.warn("OAuthError during authorize_access_token", provider=provider, error=str(e))
        code = request.query_params.get("code")
        if not code:
            return RedirectResponse(url=f"{_frontend_base()}/?error=oauth", status_code=302)
        token = await _manual_token_exchange(provider, client, code)

    if not token:
        return RedirectResponse(url=f"{_frontend_base()}/?error=oauth", status_code=302)

    access_token = token.get("access_token") if isinstance(token, dict) else None
    userinfo = await _fetch_userinfo(provider, client, token)
    identity = _identity_from_userinfo(provider, userinfo)

    if not identity.get("sub") or not identity.get("email"):
        log.warn("oauth identity missing sub/email", provider=provider)
        return RedirectResponse(url=f"{_frontend_base()}/?error=identity", status_code=302)

    # Provider-specific restrictions
    try:
        if provider == "google":
            _deny_google(identity)
        elif provider == "microsoft":
            _deny_microsoft(identity)
        elif provider == "github":
            org_resp = await client.get("user/orgs", token=token)
            if org_resp.status_code != 200:
                log.warn("github org fetch failed", status=org_resp.status_code)
                return _render_access_denied("Access denied", "Unable to verify GitHub organization membership.")
            orgs = [o["login"].lower() for o in org_resp.json() if isinstance(o, dict) and o.get("login")]
            _deny_github(identity, orgs)
    except HTTPException:
        raise
    except Exception as e:
        log.error("access denied check failed", provider=provider, error=str(e))
        return RedirectResponse(url=f"{_frontend_base()}/?error=access_denied", status_code=302)

    jwt_token = mint_access_token(identity)
    record_auth_event(event="login", provider=provider, outcome="success")
    log.info("oauth login success", provider=provider, sub=identity["sub"])

    body = (
        "<!doctype html><html><head><meta charset='utf-8'></head><body>"
        "<script>"
        f"localStorage.setItem('app_jwt', {json.dumps(jwt_token)});"
        f"window.location.replace({json.dumps(_frontend_base() + '/')});"
        "</script></body></html>"
    )
    return HTMLResponse(body)


async def _manual_token_exchange(provider: str, client, code: str) -> dict[str, Any]:
    """Fallback token exchange using direct HTTP POST."""
    token_endpoint = client.server_metadata.get("token_endpoint")
    if not token_endpoint and provider == "microsoft":
        token_endpoint = f"https://login.microsoftonline.com/{MS_TENANT_ID or 'common'}/oauth2/v2.0/token"
    if not token_endpoint:
        log.warn("no token endpoint for manual exchange", provider=provider)
        return {}

    redirect_uri = _redirect_uri(provider)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if provider == "google":
        data["client_id"] = GOOGLE_CLIENT_ID
        if GOOGLE_CLIENT_SECRET:
            data["client_secret"] = GOOGLE_CLIENT_SECRET
    elif provider == "microsoft":
        data["client_id"] = MS_CLIENT_ID
        if MS_CLIENT_SECRET:
            data["client_secret"] = MS_CLIENT_SECRET

    try:
        async with httpx.AsyncClient(timeout=15.0) as h:
            resp = await h.post(token_endpoint, data=data, headers={"Accept": "application/json"})
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        log.error("manual token exchange failed", provider=provider, error=str(e))
        return {}


@app.get("/success", response_class=HTMLResponse)
async def success_page():
    return HTMLResponse(
        "<!doctype html><html><body>"
        f"<script>window.location.replace({json.dumps(_frontend_base() + '/')});</script>"
        "</body></html>"
    )


@app.get("/logout", response_class=HTMLResponse)
@limiter.limit(limits.auth_logout, key_func=_auth_ip_key)
async def logout(request: Request):
    try:
        _session(request).clear()
    except Exception:
        pass
    return HTMLResponse(
        "<!doctype html><html><body>"
        "<script>localStorage.removeItem('app_jwt');"
        f"window.location.replace({json.dumps(_frontend_base() + '/')});</script>"
        "</body></html>"
    )


@app.get("/me")
@limiter.limit(limits.auth_me)
async def me(request: Request):
    token_text = _extract_bearer_token(request)
    try:
        claims = verify_access_token(token_text)
    except ExpiredTokenError:
        raise HTTPException(status_code=401, detail="Token expired")
    except (ClaimError, JoseError):
        raise HTTPException(status_code=401, detail="Invalid token")
    except Exception:
        log.exception("token verification failed")
        raise HTTPException(status_code=401, detail="Invalid token")
    return JSONResponse({"authenticated": True, "user": claims})


@app.get("/health")
async def health():
    return JSONResponse({
        "status": "ok",
        "service": "stateless-openid-auth",
        "providers": _enabled_providers(),
        "jwt_alg": JWT_ALG,
        "jwt_kid": JWT_KID,
        "jwks_uri": "/.well-known/jwks.json",
    })


@app.get("/.well-known/jwks.json")
@app.get("/jwks.json")
async def jwks():
    return JSONResponse(PUBLIC_JWKS)


if __name__ == "__main__":
    uvicorn.run(
        "stateless_openid_auth:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        loop="uvloop" if USE_UVLOOP else "auto",
        http="httptools",
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=False,
    )