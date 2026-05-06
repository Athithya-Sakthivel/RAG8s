from __future__ import annotations

import os


def parse_bool_env(value, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_int_env(value, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(str(value).strip())
    except Exception:
        return default


def parse_list_env(value) -> list[str]:
    if not value:
        return []
    items: list[str] = []
    for part in str(value).split(","):
        item = part.strip()
        if item:
            items.append(item)
    return items


def norm_url(value, default: str) -> str:
    if not value:
        return default
    s = str(value).strip()
    if not s:
        return default
    if s.endswith("/"):
        s = s[:-1]
    if "://" not in s:
        if s.startswith("localhost") or s.startswith("127.") or s.startswith("0.0.0.0") or ":" in s:
            s = "http://" + s
        else:
            s = "https://" + s
    return s


def norm_path(value, default: str) -> str:
    s = str(value).strip() if value else ""
    if not s:
        return default
    if not s.startswith("/"):
        s = "/" + s
    return s


SERVICE_NAME = (os.getenv("SERVICE_NAME") or "frontend").strip()
ENV = (os.getenv("ENV") or "STAGING").strip().upper()
DEPLOYMENT_ENVIRONMENT = ENV  # aligned with retriever

FRONTEND_HOSTNAME = (os.getenv("FRONTEND_HOSTNAME") or "").strip()
DEFAULT_LOCAL = "http://127.0.0.1:8000"
if FRONTEND_HOSTNAME:
    EXTERNAL_BASE = norm_url(f"https://{FRONTEND_HOSTNAME}", DEFAULT_LOCAL)
else:
    EXTERNAL_BASE = norm_url(
        os.getenv("FRONTEND_BASE") or os.getenv("FRONTEND_URL") or DEFAULT_LOCAL,
        DEFAULT_LOCAL,
    )

# Default retriever URL matches Kubernetes service name "retriever" in inference namespace
QUERY_URL = norm_url(
    os.getenv("QUERY_URL")
    or os.getenv("RETRIEVER_URL")
    or "http://retriever.inference.svc.cluster.local:8001",
    "http://retriever.inference.svc.cluster.local:8001",
)

GENERATE_STREAM_PATH = norm_path(os.getenv("GENERATE_STREAM_PATH"), "/generate/stream")
GENERATE_STREAM_URL = norm_url(
    os.getenv("GENERATE_STREAM_URL") or f"{QUERY_URL}{GENERATE_STREAM_PATH}",
    f"{QUERY_URL}{GENERATE_STREAM_PATH}",
)

STATIC_URL_PREFIX = norm_path(os.getenv("STATIC_URL_PREFIX"), "/static")
JWKS_PATH = norm_path(os.getenv("JWKS_PATH"), "/.well-known/jwks.json")
JWKS_URL = norm_url(
    os.getenv("JWKS_URL") or f"{EXTERNAL_BASE.rstrip('/')}{JWKS_PATH}",
    f"{EXTERNAL_BASE.rstrip('/')}{JWKS_PATH}",
)

REQUIRE_AUTH = parse_bool_env(os.getenv("REQUIRE_AUTH"), False)
DISPLAY_SOURCES_IN_UI = parse_bool_env(os.getenv("DISPLAY_SOURCES_IN_UI"), True)
DISPLAY_TOPK_IN_UI = parse_bool_env(os.getenv("DISPLAY_TOPK_IN_UI"), True)

COOKIE_NAME = (os.getenv("COOKIE_NAME") or "app_session").strip()
COOKIE_SAMESITE = (os.getenv("COOKIE_SAMESITE") or "lax").strip().lower()
if COOKIE_SAMESITE not in {"lax", "strict", "none"}:
    COOKIE_SAMESITE = "lax"

_cookie_secure_raw = os.getenv("COOKIE_SECURE")
if _cookie_secure_raw is not None:
    COOKIE_SECURE = parse_bool_env(_cookie_secure_raw, False)
else:
    COOKIE_SECURE = EXTERNAL_BASE.lower().startswith("https://")

SESSION_SECRET = (os.getenv("SESSION_SECRET") or "").strip()

ENABLE_GOOGLE = parse_bool_env(os.getenv("ENABLE_GOOGLE_AUTH"), False)
ENABLE_MICROSOFT = parse_bool_env(os.getenv("ENABLE_MICROSOFT_AUTH"), False)
ENABLE_GITHUB = parse_bool_env(os.getenv("ENABLE_GITHUB_AUTH"), False)

GOOGLE_CLIENT_ID = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
GOOGLE_CLIENT_SECRET = (os.getenv("GOOGLE_CLIENT_SECRET") or "").strip()
GOOGLE_ALLOWED_DOMAINS: set[str] = {
    s.strip().lower() for s in parse_list_env(os.getenv("GOOGLE_ALLOWED_DOMAINS"))
}

MS_CLIENT_ID = (os.getenv("MS_CLIENT_ID") or os.getenv("AZURE_CLIENT_ID") or "").strip()
MS_CLIENT_SECRET = (os.getenv("MS_CLIENT_SECRET") or os.getenv("AZURE_CLIENT_SECRET") or "").strip()
MS_TENANT_ID = (os.getenv("MS_TENANT_ID") or os.getenv("AZURE_TENANT_ID") or "common").strip()
MICROSOFT_ALLOWED_DOMAINS: set[str] = {
    s.strip().lower() for s in parse_list_env(os.getenv("MICROSOFT_ALLOWED_DOMAINS"))
}
MICROSOFT_ALLOWED_TENANT_IDS: set[str] = {
    s.strip().lower() for s in parse_list_env(os.getenv("MICROSOFT_ALLOWED_TENANT_IDS"))
}

GITHUB_CLIENT_ID = (os.getenv("GITHUB_CLIENT_ID") or "").strip()
GITHUB_CLIENT_SECRET = (os.getenv("GITHUB_CLIENT_SECRET") or "").strip()
GITHUB_ALLOWED_ORGS: set[str] = {
    s.strip().lower() for s in parse_list_env(os.getenv("GITHUB_ALLOWED_ORGS"))
}

GOOGLE_REDIRECT_URI = (os.getenv("GOOGLE_REDIRECT_URI") or "").strip()
MS_REDIRECT_URI = (os.getenv("MS_REDIRECT_URI") or "").strip()
GITHUB_REDIRECT_URI = (os.getenv("GITHUB_REDIRECT_URI") or "").strip()

JWT_ISS = (os.getenv("JWT_ISS") or "stateless-openid-auth").strip()
JWT_AUD = (os.getenv("JWT_AUD") or "rag-ui").strip()
JWT_ALG = (os.getenv("JWT_ALG") or "ES256").strip().upper()
JWT_TTL_SECONDS = parse_int_env(os.getenv("JWT_TTL_SECONDS"), 900)
JWT_CLOCK_SKEW_SECONDS = parse_int_env(os.getenv("JWT_CLOCK_SKEW_SECONDS"), 90)
JWT_KID = (os.getenv("JWT_KID") or "").strip()
JWT_PRIVATE_KEY_PEM = (os.getenv("JWT_PRIVATE_KEY_PEM") or "").strip()
JWT_PRIVATE_KEY_PATH = (os.getenv("JWT_PRIVATE_KEY_PATH") or "").strip()

VALKEY_URL = (os.getenv("VALKEY_URL") or os.getenv("REDIS_URL") or "").strip()

RATE_LIMIT_AUTH_LOGIN = (os.getenv("RATE_LIMIT_AUTH_LOGIN") or "5/minute").strip()
RATE_LIMIT_AUTH_START = (os.getenv("RATE_LIMIT_AUTH_START") or "5/minute").strip()
RATE_LIMIT_AUTH_CALLBACK = (os.getenv("RATE_LIMIT_AUTH_CALLBACK") or "10/minute").strip()
RATE_LIMIT_AUTH_ME = (os.getenv("RATE_LIMIT_AUTH_ME") or "60/minute").strip()
RATE_LIMIT_AUTH_LOGOUT = (os.getenv("RATE_LIMIT_AUTH_LOGOUT") or "20/minute").strip()
RATE_LIMIT_JWKS = (os.getenv("RATE_LIMIT_JWKS") or "120/minute").strip()
RATE_LIMIT_GENERATE_STREAM_AUTH = (os.getenv("RATE_LIMIT_GENERATE_STREAM_AUTH") or "10/minute").strip()
RATE_LIMIT_GENERATE_STREAM_ANON = (os.getenv("RATE_LIMIT_GENERATE_STREAM_ANON") or "2/minute").strip()
RATE_LIMIT_GENERATE_STREAM_CONCURRENCY = parse_int_env(
    os.getenv("RATE_LIMIT_GENERATE_STREAM_CONCURRENCY"), 2
)

UPSTREAM_TIMEOUT_SECONDS = parse_int_env(os.getenv("UPSTREAM_TIMEOUT_SECONDS"), 60)
UPSTREAM_PRESIGN_TIMEOUT_SECONDS = parse_int_env(os.getenv("UPSTREAM_PRESIGN_TIMEOUT_SECONDS"), 20)

# Use uvloop if available (recommended for production)
USE_UVLOOP = parse_bool_env(os.getenv("USE_UVLOOP"), True)

if JWT_TTL_SECONDS <= 0:
    raise RuntimeError("JWT_TTL_SECONDS must be positive")

if JWT_CLOCK_SKEW_SECONDS < 0:
    raise RuntimeError("JWT_CLOCK_SKEW_SECONDS must be zero or positive")

if RATE_LIMIT_GENERATE_STREAM_CONCURRENCY <= 0:
    raise RuntimeError("RATE_LIMIT_GENERATE_STREAM_CONCURRENCY must be positive")


def get_redirect(provider: str) -> str:
    p = (provider or "").strip().lower()
    base = EXTERNAL_BASE.rstrip("/")

    if p == "google" and GOOGLE_REDIRECT_URI:
        return GOOGLE_REDIRECT_URI
    if p == "microsoft" and MS_REDIRECT_URI:
        return MS_REDIRECT_URI
    if p == "github" and GITHUB_REDIRECT_URI:
        return GITHUB_REDIRECT_URI

    if base.endswith("/auth/callback"):
        return f"{base}/{p}"
    return f"{base}/auth/callback/{p}"


def enabled_flags():
    return {
        "google": ENABLE_GOOGLE,
        "microsoft": ENABLE_MICROSOFT,
        "github": ENABLE_GITHUB,
    }


def enabled_providers_effective():
    out = []
    if ENABLE_GOOGLE and GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        out.append("google")
    if ENABLE_MICROSOFT and MS_CLIENT_ID and MS_CLIENT_SECRET:
        out.append("microsoft")
    if ENABLE_GITHUB and GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET:
        out.append("github")
    return out


def has_jwt_signing_material() -> bool:
    return bool(JWT_PRIVATE_KEY_PEM or JWT_PRIVATE_KEY_PATH)