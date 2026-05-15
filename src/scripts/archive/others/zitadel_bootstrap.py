#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Any, NoReturn

import yaml

DOMAIN = (os.environ.get("DOMAIN", "athithya.site") or "athithya.site").strip().rstrip(".") or "athithya.site"
AUTH_HOST = (os.environ.get("AUTH_HOST", f"auth.{DOMAIN}") or f"auth.{DOMAIN}").strip().rstrip(".") or f"auth.{DOMAIN}"
NAMESPACE = (os.environ.get("ZITADEL_NAMESPACE", "zitadel") or "zitadel").strip() or "zitadel"

MASTERKEY_SECRET_NAME = (os.environ.get("ZITADEL_MASTERKEY_SECRET_NAME", "zitadel-masterkey") or "zitadel-masterkey").strip()
CONFIG_SECRET_NAME = (os.environ.get("ZITADEL_CONFIG_SECRET_NAME", "zitadel-config-secret") or "zitadel-config-secret").strip()

VERBOSE = (os.environ.get("VERBOSE", "0") or "0").strip().lower() in {"1", "true", "yes", "y", "on"}


class Dumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


Dumper.add_representer(str, _str_representer)


def yaml_dump(data: Any) -> str:
    return yaml.dump(
        data,
        Dumper=Dumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=120,
        indent=2,
    )


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def log(*parts: object) -> None:
    print(*parts, flush=True)


def dbg(*parts: object) -> None:
    if VERBOSE:
        log(*parts)


def fatal(msg: str) -> NoReturn:
    raise SystemExit(f"ERROR: {msg}")


def env(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def require_cmd(name: str) -> None:
    if which(name) is None:
        fatal(f"Required command not found: {name}")


def run_cmd(args: list[str], *, stdin: str | None = None) -> None:
    dbg("RUN:", " ".join(args))
    subprocess.run(args, input=stdin, text=True, check=True)


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


@dataclass(frozen=True)
class Config:
    masterkey: str
    admin_password: str
    database_dsn: str
    instance_name: str
    admin_email: str


def load_config() -> Config:
    return Config(
        masterkey=env("ZITADEL_MASTERKEY", ""),
        admin_password=env("ZITADEL_FIRSTINSTANCE_ORG_HUMAN_PASSWORD", ""),
        database_dsn=env("ZITADEL_DATABASE_POSTGRES_DSN", ""),
        instance_name=env("ZITADEL_INSTANCE_NAME", "athithya"),
        admin_email=env("ZITADEL_ADMIN_EMAIL", f"admin@{DOMAIN}"),
    )


def validate(cfg: Config) -> None:
    if len(cfg.masterkey.encode("utf-8")) != 32:
        fatal("ZITADEL_MASTERKEY must be exactly 32 bytes")

    if not re.match(r"^postgres(ql)?://", cfg.database_dsn):
        fatal("ZITADEL_DATABASE_POSTGRES_DSN must start with postgresql:// or postgres://")

    if not cfg.admin_password:
        fatal("ZITADEL_FIRSTINSTANCE_ORG_HUMAN_PASSWORD cannot be empty")

    if not cfg.admin_email:
        fatal("ZITADEL_ADMIN_EMAIL cannot be empty")


def render_config_secret(cfg: Config) -> dict[str, Any]:
    config_yaml = {
        "Database": {
            "Postgres": {
                "DSN": cfg.database_dsn,
            }
        },
        "FirstInstance": {
            "InstanceName": cfg.instance_name,
            "DefaultLanguage": "en",
            "Org": {
                "Name": cfg.instance_name,
                "Human": {
                    "UserName": "admin",
                    "FirstName": "admin",
                    "LastName": "admin",
                    "Email": {
                        "Address": cfg.admin_email,
                        "Verified": True,
                    },
                    "Password": cfg.admin_password,
                    "PasswordChangeRequired": True,
                },
            },
        },
    }

    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": CONFIG_SECRET_NAME,
            "namespace": NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": "zitadel",
                "app.kubernetes.io/component": "bootstrap",
            },
        },
        "type": "Opaque",
        "stringData": {
            "config-yaml": yaml_dump(config_yaml).rstrip() + "\n",
        },
    }


def render_masterkey_secret(cfg: Config) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": MASTERKEY_SECRET_NAME,
            "namespace": NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": "zitadel",
                "app.kubernetes.io/component": "bootstrap",
            },
        },
        "type": "Opaque",
        "stringData": {
            "masterkey": cfg.masterkey,
        },
    }


def render_payload(cfg: Config) -> str:
    docs = [render_masterkey_secret(cfg), render_config_secret(cfg)]
    return "\n---\n".join(yaml_dump(doc).rstrip() for doc in docs) + "\n"


def apply_secrets(cfg: Config) -> None:
    ensure_namespace(NAMESPACE)
    run_cmd(["kubectl", "apply", "-f", "-"], stdin=render_payload(cfg))


def delete_secrets() -> None:
    require_cmd("kubectl")
    for secret_name in [MASTERKEY_SECRET_NAME, CONFIG_SECRET_NAME]:
        run_cmd(
            [
                "kubectl",
                "delete",
                "secret",
                secret_name,
                "-n",
                NAMESPACE,
                "--ignore-not-found=true",
            ]
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Seed Zitadel base configuration secrets.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--destroy", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config()
    validate(cfg)

    if args.write:
        print(render_payload(cfg), end="")
        return

    if args.destroy:
        delete_secrets()
        log(f"Deleted bootstrap configuration in namespace {NAMESPACE}")
        return

    apply_secrets(cfg)
    log(f"Applied bootstrap configuration in namespace {NAMESPACE}")
    log(f"Masterkey secret: {NAMESPACE}/{MASTERKEY_SECRET_NAME}")
    log(f"Runtime config secret: {NAMESPACE}/{CONFIG_SECRET_NAME}")


if __name__ == "__main__":
    main()