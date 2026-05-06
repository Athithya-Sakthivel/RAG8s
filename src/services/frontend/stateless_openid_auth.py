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

import uvicorn
from authlib.integrations.starlette_client import OAuth, OAuthError
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
    enabled_providers_effective,
    get_redirect,
    has_jwt_signing_material,
    USE_UVLOOP,
)
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from joserfc import jwk, jwt
from joserfc.errors import ClaimError, ExpiredTokenError, InvalidClaimError, JoseError
from joserfc.jwk import ECKey
from joserfc.jwt import JWTClaimsRegistry
from starlette.middleware.sessions import SessionMiddleware

logger = logging.getLogger(__name__)

if JWT_ALG != "ES256":
    raise RuntimeError(f"Unsupported JWT_ALG={JWT_ALG!r}. This service is pinned to ES256.")

if not SESSION_SECRET:
    raise RuntimeError("SESSION_SECRET is required for the transient OIDC login flow.")

if not has_jwt_signing_material():
    raise RuntimeError("JWT signing material is required. Set JWT_PRIVATE_KEY_PEM or JWT_PRIVATE_KEY_PATH.")

if not JWT_KID:
    raise RuntimeError("JWT_KID is required so signed tokens and JWKS remain stable across rotations.")

app = FastAPI(title="stateless-openid-auth", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie=COOKIE_NAME,
    same_site=COOKIE_SAMESITE,
    https_only=COOKIE_SECURE,
)


def _load_private_key():
    raw = (JWT_PRIVATE_KEY_PEM or "").strip()
    if not raw and JWT_PRIVATE_KEY_PATH:
        path = Path(JWT_PRIVATE_KEY_PATH)
        raw = path.read_text(encoding="utf-8").strip()

    if not raw:
        raise RuntimeError("JWT signing key is missing.")

    if raw.lstrip().startswith("{"):
        key = jwk.import_key(json.loads(raw))
    else:
        key = ECKey.import_key(raw)

    return key


SIGNING_KEY = _load_private_key()
PUBLIC_KEY = ECKey.import_key(SIGNING_KEY.as_dict(private=False))
PUBLIC_JWK = PUBLIC_KEY.as_dict(private=False)
PUBLIC_JWK["kid"] = JWT_KID
PUBLIC_JWKS = {"keys": [PUBLIC_JWK]}


class _SkewClaimsRegistry(JWTClaimsRegistry):
    def validate_exp(self, value):
        if not isinstance(value, (int, float)):
            raise InvalidClaimError("exp", "The exp claim must be a timestamp")
        if _jwt_now().timestamp() > float(value) + JWT_CLOCK_SKEW_SECONDS:
            raise ExpiredTokenError("exp")

    def validate_iat(self, value):
        if not isinstance(value, (int, float)):
            raise InvalidClaimError("iat", "The iat claim must be a timestamp")
        if _jwt_now().timestamp() + JWT_CLOCK_SKEW_SECONDS < float(value):
            raise InvalidClaimError("iat", "The token is not yet valid")

    def validate_nbf(self, value):
        if not isinstance(value, (int, float)):
            raise InvalidClaimError("nbf", "The nbf claim must be a timestamp")
        if _jwt_now().timestamp() + JWT_CLOCK_SKEW_SECONDS < float(value):
            raise InvalidClaimError("nbf", "The token is not yet valid")


CLAIMS_REQUESTS = _SkewClaimsRegistry(
    iss={"essential": True, "value": JWT_ISS},
    aud={"essential": True, "value": JWT_AUD},
    sub={"essential": True},
    exp={"essential": True},
    iat={"essential": True},
)

oauth = OAuth()
ENABLED_PROVIDERS = enabled_providers_effective()

if ENABLE_GOOGLE and "google" in ENABLED_PROVIDERS:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid email profile",
            "code_challenge_method": "S256",
        },
    )

if ENABLE_MICROSOFT and "microsoft" in ENABLED_PROVIDERS:
    microsoft_tenant = MS_TENANT_ID or "common"
    oauth.register(
        name="microsoft",
        client_id=MS_CLIENT_ID,
        client_secret=MS_CLIENT_SECRET,
        server_metadata_url=f"https://login.microsoftonline.com/{microsoft_tenant}/v2.0/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid email profile offline_access User.Read",
            "code_challenge_method": "S256",
        },
    )

if ENABLE_GITHUB and "github" in ENABLED_PROVIDERS:
    oauth.register(
        name="github",
        client_id=GITHUB_CLIENT_ID,
        client_secret=GITHUB_CLIENT_SECRET,
        access_token_url="https://github.com/login/oauth/access_token",
        authorize_url="https://github.com/login/oauth/authorize",
        api_base_url="https://api.github.com/",
        client_kwargs={
            "scope": "user:email read:org",
            "code_challenge_method": "S256",
        },
    )


def _enabled_providers() -> list[str]:
    return enabled_providers_effective()


def _frontend_base() -> str:
    return EXTERNAL_BASE.rstrip("/")


def _redirect_uri(provider: str) -> str:
    return get_redirect(provider)


def _provider_client(provider: str):
    client = oauth.create_client(provider)
    if client is None:
        raise HTTPException(status_code=500, detail=f"OAuth client unavailable for provider '{provider}'")
    return client


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


def _render_index(status_html: str = "") -> HTMLResponse:
    providers = _enabled_providers()
    if providers:
        buttons = "".join(
            f"<li><a href='/login/start/{html.escape(p)}'>{html.escape(p.capitalize())}</a></li>"
            for p in providers
        )
        provider_html = f"<ul>{buttons}</ul>"
    else:
        provider_html = "<p>No providers are enabled.</p>"

    body = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in</title></head>
<body>
  <h1>Sign in</h1>
  {provider_html}
  <p><code>{html.escape(_frontend_base())}</code></p>
  <p><code>{html.escape(status_html)}</code></p>
</body>
</html>"""
    return HTMLResponse(body)


@app.get("/redirects", response_class=HTMLResponse)
async def redirects_page():
    providers = _enabled_providers()
    rows = "".join(
        f"<li><strong>{html.escape(p)}</strong>: <code>{html.escape(_redirect_uri(p))}</code></li>"
        for p in providers
    )
    body = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Redirect URIs</title></head>
<body>
<h1>Redirect URIs to register</h1>
<ul>{rows}</ul>
</body>
</html>"""
    return HTMLResponse(body)


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return _render_index()


@app.get("/login/start/{provider}")
async def login_start(request: Request, provider: str):
    provider = (provider or "").strip().lower()
    if provider not in _enabled_providers():
        raise HTTPException(status_code=404, detail="Provider not enabled")

    client = _provider_client(provider)
    redirect_uri = _redirect_uri(provider)

    sess = _session(request)
    sess["oauth_provider"] = provider
    sess["oauth_started_at"] = int(_jwt_now().timestamp())
    # Generate and store a random state to prevent CSRF
    state = secrets.token_urlsafe(32)
    sess["oauth_state"] = state

    try:
        return await client.authorize_redirect(request, redirect_uri, state=state)
    except OAuthError as exc:
        logger.warning("oauth redirect failed for %s: %s", provider, exc)
        raise HTTPException(status_code=502, detail="OAuth redirect initiation failed")
    except Exception as exc:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        logger.error("oauth redirect failed for %s\n%s", provider, tb)
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
        if not isinstance(data, dict):
            return {}

        email = data.get("email")
        if not email:
            resp2 = await client.get("user/emails", token=token)
            if resp2.status_code == 200:
                emails = resp2.json()
                if isinstance(emails, list):
                    primary = None
                    verified = None
                    for entry in emails:
                        if not isinstance(entry, dict):
                            continue
                        if entry.get("primary") and entry.get("verified") and entry.get("email"):
                            primary = entry.get("email")
                            break
                        if entry.get("verified") and entry.get("email") and verified is None:
                            verified = entry.get("email")
                    email = primary or verified

        return {
            "id": data.get("id"),
            "node_id": data.get("node_id"),
            "login": data.get("login"),
            "name": data.get("name"),
            "email": email,
        }

    return {}


def _deny_google(identity: dict[str, Any]) -> None:
    if not GOOGLE_ALLOWED_DOMAINS:
        return
    domain = _safe_email_domain(identity.get("email"))
    if not domain or domain not in GOOGLE_ALLOWED_DOMAINS:
        raise HTTPException(status_code=403, detail="Access denied")


def _deny_microsoft(identity: dict[str, Any]) -> None:
    if MICROSOFT_ALLOWED_TENANT_IDS:
        tenant = (identity.get("tenant") or "").strip().lower() if identity.get("tenant") else None
        if not tenant or tenant not in MICROSOFT_ALLOWED_TENANT_IDS:
            raise HTTPException(status_code=403, detail="Access denied")

    if MICROSOFT_ALLOWED_DOMAINS:
        domain = _safe_email_domain(identity.get("email"))
        if not domain or domain not in MICROSOFT_ALLOWED_DOMAINS:
            raise HTTPException(status_code=403, detail="Access denied")


def _deny_github(identity: dict[str, Any], orgs: list[str]) -> None:
    if GITHUB_ALLOWED_ORGS and not any(org in GITHUB_ALLOWED_ORGS for org in orgs):
        raise HTTPException(status_code=403, detail="Access denied")


@app.get("/callback/{provider}")
async def callback(request: Request, provider: str):
    provider = (provider or "").strip().lower()
    if provider not in _enabled_providers():
        raise HTTPException(status_code=404, detail="Provider not enabled")

    client = _provider_client(provider)

    # Validate state to prevent CSRF
    sess = _session(request)
    stored_state = sess.pop("oauth_state", None)
    request_state = request.query_params.get("state")
    if not stored_state or stored_state != request_state:
        logger.warning("oauth state mismatch for provider=%s", provider)
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    try:
        token = await client.authorize_access_token(request)
    except OAuthError as exc:
        logger.warning("oauth callback failed for %s: %s", provider, exc)
        return RedirectResponse(url=f"{_frontend_base()}/?error=oauth", status_code=302)
    except Exception as exc:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        logger.error("oauth callback failed for %s\n%s", provider, tb)
        return RedirectResponse(url=f"{_frontend_base()}/?error=oauth", status_code=302)

    if not isinstance(token, dict):
        return RedirectResponse(url=f"{_frontend_base()}/?error=oauth", status_code=302)

    userinfo = await _fetch_userinfo(provider, client, token)
    identity = _identity_from_userinfo(provider, userinfo)

    if not identity.get("sub") or not identity.get("email"):
        return RedirectResponse(url=f"{_frontend_base()}/?error=identity", status_code=302)

    if provider == "google":
        _deny_google(identity)
    elif provider == "microsoft":
        _deny_microsoft(identity)
    elif provider == "github":
        org_resp = await client.get("user/orgs", token=token)
        if org_resp.status_code != 200:
            raise HTTPException(status_code=403, detail="Access denied")
        orgs = []
        org_payload = org_resp.json()
        if isinstance(org_payload, list):
            for item in org_payload:
                if isinstance(item, dict) and isinstance(item.get("login"), str):
                    orgs.append(item["login"].lower())
        _deny_github(identity, orgs)

    access_token = mint_access_token(identity)
    logger.info("oauth callback success provider=%s sub=%s", provider, identity.get("sub"))

    body = f"""<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body>
<script>
try {{
  localStorage.setItem('app_jwt', {json.dumps(access_token)});
}} catch (e) {{}}
window.location.replace({json.dumps(_frontend_base())});
</script>
</body>
</html>"""
    return HTMLResponse(body)


@app.get("/success", response_class=HTMLResponse)
async def success_page():
    return HTMLResponse(
        f"<!doctype html><html><body><script>window.location.replace({json.dumps(_frontend_base())});</script></body></html>"
    )


@app.get("/logout", response_class=HTMLResponse)
async def logout(request: Request):
    try:
        _session(request).clear()
    except Exception:
        pass

    body = f"""<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body>
<script>
try {{ localStorage.removeItem('app_jwt'); }} catch (e) {{}}
window.location.replace({json.dumps(_frontend_base())});
</script>
</body>
</html>"""
    return HTMLResponse(body)


@app.get("/me")
async def me(request: Request):
    token_text = _extract_bearer_token(request)
    try:
        claims = verify_access_token(token_text)
    except ExpiredTokenError:
        raise HTTPException(status_code=401, detail="Token expired")
    except (ClaimError, JoseError):
        raise HTTPException(status_code=401, detail="Invalid token")
    except Exception as exc:
        logger.error("token verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid token")

    return {"authenticated": True, "user": claims}


@app.get("/health")
async def health():
    return JSONResponse(
        {
            "status": "ok",
            "service": "stateless-openid-auth",
            "providers": _enabled_providers(),
            "jwt_alg": JWT_ALG,
            "jwt_kid": JWT_KID,
            "jwks_uri": "/.well-known/jwks.json",
        }
    )


@app.get("/.well-known/jwks.json")
@app.get("/jwks.json")
async def jwks():
    return JSONResponse(PUBLIC_JWKS)


def _decode_access_token(token_text: str) -> dict[str, Any]:
    return verify_access_token(token_text)


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    loop = "uvloop" if USE_UVLOOP else "auto"
    uvicorn.run(
        "stateless_openid_auth:app",
        host=host,
        port=port,
        loop=loop,
        http="httptools",
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=False,
    )