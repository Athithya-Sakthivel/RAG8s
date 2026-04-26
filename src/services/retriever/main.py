#!/usr/bin/env python3
from __future__ import annotations

import os

import uvicorn
from retriever import create_app
from telemetry import setup_logging

setup_logging()
app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8001")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        loop=os.getenv("UVICORN_LOOP", "uvloop"),
        http=os.getenv("UVICORN_HTTP", "httptools"),
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "*"),
    )
