#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Any, NoReturn

import yaml

DOMAIN = (os.environ.get("DOMAIN", "athithya.site") or "athithya.site").strip().rstrip(".") or "athithya.site"
AUTH_HOST = (os.environ.get("AUTH_HOST", f"auth.{DOMAIN}") or f"auth.{DOMAIN}").strip().rstrip(".") or f"auth.{DOMAIN}"
ISSUER = (os.environ.get("ZITADEL_ISSUER_URL", f"https://{AUTH_HOST}") or f"https://{AUTH_HOST}").strip().rstrip("/")
ZITADEL_NAMESPACE = (os.environ.get("ZITADEL_NAMESPACE", "zitadel") or "zitadel").strip() or "zitadel"
RETRIEVER_NAMESPACE = (os.environ.get("RETRIEVER_NAMESPACE", "inference") or "inference").strip() or "inference"

BOOTSTRAP_PAT_SECRET_NAME = (os.environ.get("ZITADEL_BOOTSTRAP_PAT_SECRET_NAME", "zitadel-bootstrap-pat") or "zitadel-bootstrap-pat").strip()
POTENTIAL_PAT_SOURCE_SECRETS = tuple(
    s.strip()
    for s in os.environ.get(
        "ZITADEL_PAT_SOURCE_SECRETS",
        "login-client,zitadel-admin-credentials,iam-admin,iam-owner,zitadel-bootstrap-pat",
    ).split(",")
    if s.strip()
)

GOOGLE_OAUTH_CLIENT_ID = (os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "") or "").strip()
GOOGLE_OAUTH_CLIENT_SECRET = (os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "") or "").strip()
GOOGLE_REDIRECT_URI = (os.environ.get("GOOGLE_REDIRECT_URI", f"{ISSUER}/idps/callback") or f"{ISSUER}/idps/callback").strip()

RETRIEVER_PROJECT_NAME = (os.environ.get("RETRIEVER_PROJECT_NAME", "retriever") or "retriever").strip()
RETRIEVER_APP_NAME = (os.environ.get("RETRIEVER_APP_NAME", "retriever") or "retriever").strip()
RETRIEVER_REDIRECT_URI = (
    os.environ.get("RETRIEVER_REDIRECT_URI", f"https://api.{DOMAIN}/auth/callback") or f"https://api.{DOMAIN}/auth/callback"
).strip()
RETRIEVER_LOGOUT_REDIRECT_URI = (
    os.environ.get("RETRIEVER_LOGOUT_REDIRECT_URI", f"https://api.{DOMAIN}/auth/logout") or f"https://api.{DOMAIN}/auth/logout"
).strip()

ALLOW_USERNAME_PASSWORD = os.environ.get("ZITADEL_ALLOW_USERNAME_PASSWORD", "true").strip().lower() in {"1", "true", "yes", "y", "on"}
ALLOW_REGISTER = os.environ.get("ZITADEL_ALLOW_REGISTER", "false").strip().lower() in {"1", "true", "yes", "y", "on"}
ALLOW_EXTERNAL_IDP = os.environ.get("ZITADEL_ALLOW_EXTERNAL_IDP", "true").strip().lower() in {"1", "true", "yes", "y", "on"}

VERBOSE = os.environ.get("VERBOSE", "0").strip().lower() in {"1", "true", "yes", "y", "on"}


class ApiError(RuntimeError):
    pass


def log(*parts: object) -> None:
    print(*parts, flush=True)


def dbg(*parts: object) -> None:
    if VERBOSE:
        log(*parts)


def fatal(msg: str) -> NoReturn:
    raise SystemExit(f"ERROR: {msg}")


def require_cmd(name: str) -> None:
    if which(name) is None:
        fatal(f"Required command not found: {name}")


def run_cmd(args: list[str], *, stdin: str | None = None, capture: bool = False) -> str:
    dbg("RUN:", " ".join(args))
    proc = subprocess.run(
        args,
        input=stdin,
        text=True,
        check=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    return (proc.stdout or "").strip() if capture else ""


def ensure_namespace(namespace: str) -> None:
    require_cmd("kubectl")
    result = subprocess.run(
        ["kubectl", "get", "namespace", namespace],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0:
        run_cmd(["kubectl", "create", "namespace", namespace])


def secret_exists(namespace: str, name: str) -> bool:
    require_cmd("kubectl")
    result = subprocess.run(
        ["kubectl", "get", "secret", name, "-n", namespace],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.returncode == 0


def get_secret_value(namespace: str, secret_name: str, key: str) -> str:
    return run_cmd(
        [
            "kubectl",
            "-n",
            namespace,
            "get",
            "secret",
            secret_name,
            "-o",
            f"jsonpath={{.data.{key}}}",
        ],
        capture=True,
    )


def decode_b64_text(value: str) -> str:
    return base64.b64decode(value.encode("utf-8")).decode("utf-8").strip()


def load_pat() -> tuple[str, str]:
    pat = (os.environ.get("ZITADEL_BOOTSTRAP_PAT", "") or "").strip()
    if pat:
        return "env", pat

    pat_file = (os.environ.get("ZITADEL_BOOTSTRAP_PAT_FILE", "") or "").strip()
    if pat_file:
        path = Path(pat_file).expanduser()
        if not path.is_file():
            fatal(f"ZITADEL_BOOTSTRAP_PAT_FILE points to a missing file: {path}")
        return "file", path.read_text(encoding="utf-8").strip()

    for secret_name in POTENTIAL_PAT_SOURCE_SECRETS:
        if not secret_exists(ZITADEL_NAMESPACE, secret_name):
            continue
        for key in ("pat", "PAT", "token"):
            raw = get_secret_value(ZITADEL_NAMESPACE, secret_name, key)
            if raw:
                decoded = decode_b64_text(raw)
                if len(decoded) >= 20:
                    return f"secret:{secret_name}", decoded

    fatal(
        "No usable API token found. The first-install machine-user credential secret must exist first. "
        f"Looked for: {', '.join(POTENTIAL_PAT_SOURCE_SECRETS)}"
    )


@dataclass(frozen=True)
class ApiResult:
    status: int
    body: dict[str, Any] | list[Any] | str


class ZitadelClient:
    def __init__(self, issuer: str, token: str):
        self.issuer = issuer.rstrip("/")
        self.token = token.strip()

    def request(self, method: str, path: str, payload: Any | None = None, headers: dict[str, str] | None = None) -> ApiResult:
        url = f"{self.issuer}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req_headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if payload is not None:
            req_headers["Content-Type"] = "application/json"
        if headers:
            req_headers.update(headers)

        request = urllib.request.Request(url, data=data, headers=req_headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                raw = resp.read().decode("utf-8", errors="replace").strip()
                if not raw:
                    return ApiResult(resp.status, "")
                try:
                    return ApiResult(resp.status, json.loads(raw))
                except json.JSONDecodeError:
                    return ApiResult(resp.status, raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace").strip()
            raise ApiError(f"{method} {path} failed with HTTP {exc.code}: {raw or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"{method} {path} failed: {exc}") from exc

    def get(self, path: str, headers: dict[str, str] | None = None) -> ApiResult:
        return self.request("GET", path, headers=headers)

    def post(self, path: str, payload: Any, headers: dict[str, str] | None = None) -> ApiResult:
        return self.request("POST", path, payload=payload, headers=headers)

    def put(self, path: str, payload: Any, headers: dict[str, str] | None = None) -> ApiResult:
        return self.request("PUT", path, payload=payload, headers=headers)


def apply_secret(namespace: str, name: str, data: dict[str, str], labels: dict[str, str]) -> None:
    ensure_namespace(namespace)
    payload = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
        },
        "type": "Opaque",
        "stringData": data,
    }
    run_cmd(["kubectl", "apply", "-f", "-"], stdin=yaml.dump(payload, sort_keys=False))


def get_my_org_id(client: ZitadelClient) -> str:
    res = client.get("/management/v1/orgs/me")
    body = res.body
    if not isinstance(body, dict):
        fatal("unexpected org response shape")
    org = body.get("org")
    if not isinstance(org, dict) or not org.get("id"):
        fatal("unable to resolve organization id")
    return str(org["id"])


def ensure_login_policy(client: ZitadelClient) -> None:
    payload = {
        "allowUsernamePassword": ALLOW_USERNAME_PASSWORD,
        "allowRegister": ALLOW_REGISTER,
        "allowExternalIdp": ALLOW_EXTERNAL_IDP,
    }
    client.put("/policies/login", payload)
    log("Login policy updated")


def ensure_google_provider(client: ZitadelClient) -> tuple[str, str]:
    payload = {
        "name": "google",
        "clientId": GOOGLE_OAUTH_CLIENT_ID,
        "clientSecret": GOOGLE_OAUTH_CLIENT_SECRET,
        "scopes": ["openid", "profile", "email"],
    }
    res = client.post("/idps/google", payload)
    body = res.body
    if not isinstance(body, dict) or not body.get("id"):
        fatal("Google provider creation returned an unexpected response")
    provider_id = str(body["id"])
    provider_scope = f"urn:zitadel:iam:org:idp:id:{provider_id}"
    log(f"Google provider created: {provider_id}")
    return provider_id, provider_scope


def link_provider_to_login_policy(client: ZitadelClient, provider_id: str) -> None:
    client.post("/policies/login/idps", {"idpId": provider_id})
    log(f"Google provider linked to login policy: {provider_id}")


def ensure_project(client: ZitadelClient, org_id: str) -> str:
    payload = {
        "name": RETRIEVER_PROJECT_NAME,
    }
    headers = {
        "x-zitadel-orgid": org_id,
    }
    res = client.post("/projects", payload, headers=headers)
    body = res.body
    if not isinstance(body, dict):
        fatal("project creation returned an unexpected response")
    project_id = str(body.get("id") or body.get("projectId") or "")
    if not project_id:
        fatal("project creation did not return a project id")
    log(f"Project created: {project_id}")
    return project_id


def ensure_oidc_app(client: ZitadelClient, org_id: str, project_id: str) -> tuple[str, str, str]:
    payload = {
        "name": RETRIEVER_APP_NAME,
        "authMethodType": "OIDC_AUTH_METHOD_TYPE_BASIC",
        "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
        "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"],
        "redirectUris": [RETRIEVER_REDIRECT_URI],
        "postLogoutRedirectUris": [RETRIEVER_LOGOUT_REDIRECT_URI],
        "accessTokenType": "OIDC_TOKEN_TYPE_BEARER",
        "version": "OIDC_VERSION_1_0",
        "devMode": False,
        "accessTokenRoleAssertion": True,
        "idTokenRoleAssertion": True,
        "idTokenUserinfoAssertion": True,
    }
    headers = {
        "x-zitadel-orgid": org_id,
    }
    res = client.post(f"/projects/{project_id}/apps/oidc", payload, headers=headers)
    body = res.body
    if not isinstance(body, dict):
        fatal("OIDC application creation returned an unexpected response")

    app_id = str(body.get("appId") or body.get("id") or "")
    client_id = str(body.get("clientId") or "")
    client_secret = str(body.get("clientSecret") or "")
    if not app_id or not client_id or not client_secret:
        fatal("OIDC application creation did not return appId/clientId/clientSecret")

    log(f"OIDC application created: {app_id}")
    return app_id, client_id, client_secret


def mirror_api_token(token: str) -> None:
    apply_secret(
        ZITADEL_NAMESPACE,
        BOOTSTRAP_PAT_SECRET_NAME,
        {
            "pat": token,
        },
        {
            "app.kubernetes.io/name": "zitadel",
            "app.kubernetes.io/component": "bootstrap-pat",
        },
    )
    log(f"Canonical API token secret stored at {ZITADEL_NAMESPACE}/{BOOTSTRAP_PAT_SECRET_NAME}")


def store_retriever_secret(
    provider_id: str,
    provider_scope: str,
    org_id: str,
    project_id: str,
    app_id: str,
    client_id: str,
    client_secret: str,
) -> None:
    apply_secret(
        RETRIEVER_NAMESPACE,
        "retriever-oidc",
        {
            "AUTH_ISSUER_URL": ISSUER,
            "AUTH_CLIENT_ID": client_id,
            "AUTH_CLIENT_SECRET": client_secret,
            "AUTH_REDIRECT_URI": RETRIEVER_REDIRECT_URI,
            "AUTH_LOGOUT_REDIRECT_URI": RETRIEVER_LOGOUT_REDIRECT_URI,
            "AUTH_GOOGLE_IDP_ID": provider_id,
            "AUTH_GOOGLE_IDP_SCOPE": provider_scope,
            "ZITADEL_ORG_ID": org_id,
            "ZITADEL_PROJECT_ID": project_id,
            "ZITADEL_APP_ID": app_id,
        },
        {
            "app.kubernetes.io/name": "retriever",
            "app.kubernetes.io/component": "oidc",
        },
    )
    log(f"Retriever OIDC settings stored in {RETRIEVER_NAMESPACE}/retriever-oidc")


def wait_for_pat(timeout_seconds: int) -> tuple[str, str]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        for secret_name in POTENTIAL_PAT_SOURCE_SECRETS:
            if not secret_exists(ZITADEL_NAMESPACE, secret_name):
                continue
            for key in ("pat", "PAT", "token"):
                raw = get_secret_value(ZITADEL_NAMESPACE, secret_name, key)
                if raw:
                    decoded = decode_b64_text(raw)
                    if len(decoded) >= 20:
                        log(f"Found usable token source in {ZITADEL_NAMESPACE}/{secret_name}")
                        return secret_name, decoded
        time.sleep(5)
    fatal(
        "Timed out waiting for the first-install credential secret. "
        f"Searched: {', '.join(POTENTIAL_PAT_SOURCE_SECRETS)}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Configure Google OIDC login and the retriever client in Zitadel.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--destroy", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=1800)
    args = parser.parse_args(argv)

    if args.destroy:
        require_cmd("kubectl")
        subprocess.run(
            ["kubectl", "-n", ZITADEL_NAMESPACE, "delete", "secret", BOOTSTRAP_PAT_SECRET_NAME, "--ignore-not-found=true"],
            check=False,
            text=True,
        )
        subprocess.run(
            ["kubectl", "-n", RETRIEVER_NAMESPACE, "delete", "secret", "retriever-oidc", "--ignore-not-found=true"],
            check=False,
            text=True,
        )
        log("Removed token and OIDC settings")
        return

    if not GOOGLE_OAUTH_CLIENT_ID or not GOOGLE_OAUTH_CLIENT_SECRET:
        fatal("GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET are required")
    if not GOOGLE_REDIRECT_URI:
        fatal("GOOGLE_REDIRECT_URI is required")
    if not RETRIEVER_REDIRECT_URI:
        fatal("RETRIEVER_REDIRECT_URI is required")

    source_name, pat = load_pat()
    if source_name.startswith("secret:"):
        log(f"Using token from {source_name}")
    else:
        log(f"Using token from {source_name}")

    if len(pat) < 20:
        fatal("The API token looks invalid")

    client = ZitadelClient(ISSUER, pat)

    ensure_login_policy(client)
    org_id = get_my_org_id(client)
    provider_id, provider_scope = ensure_google_provider(client)
    link_provider_to_login_policy(client, provider_id)
    project_id = ensure_project(client, org_id)
    app_id, client_id, client_secret = ensure_oidc_app(client, org_id, project_id)

    mirror_api_token(pat)
    store_retriever_secret(provider_id, provider_scope, org_id, project_id, app_id, client_id, client_secret)

    summary = {
        "issuer": ISSUER,
        "organization_id": org_id,
        "google_provider_id": provider_id,
        "google_provider_scope": provider_scope,
        "project_id": project_id,
        "application_id": app_id,
        "retriever_client_id": client_id,
        "retriever_redirect_uri": RETRIEVER_REDIRECT_URI,
        "retriever_logout_redirect_uri": RETRIEVER_LOGOUT_REDIRECT_URI,
        "retriever_secret_namespace": RETRIEVER_NAMESPACE,
        "retriever_secret_name": "retriever-oidc",
        "bootstrap_token_secret": f"{ZITADEL_NAMESPACE}/{BOOTSTRAP_PAT_SECRET_NAME}",
    }

    if args.write:
        print(yaml.dump(summary, sort_keys=False), end="")
        return

    log("Google OIDC configuration complete")
    for key, value in summary.items():
        log(f"{key}: {value}")


if __name__ == "__main__":
    main()