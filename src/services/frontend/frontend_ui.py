# frontend_ui.py
from __future__ import annotations

import os

from config import DISPLAY_TOPK_IN_UI, REQUIRE_AUTH
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

SERVICE_NAME = (os.getenv("SERVICE_NAME") or "frontend").strip()
ENV = (os.getenv("ENV") or "STAGING").strip().upper()

app = FastAPI(title="frontend-ui", docs_url=None, redoc_url=None, openapi_url=None)

_INDEX_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>RAG UI</title>
  <style>
    :root { color-scheme: light; }
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 0; background: #f6f7fb; color: #111827; }
    .wrap { max-width: 1100px; margin: 0 auto; padding: 24px; }
    .card { background: white; border: 1px solid #e5e7eb; border-radius: 12px; padding: 20px; box-shadow: 0 1px 2px rgba(0,0,0,.03); }
    .top { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 16px; }
    h1 { font-size: 22px; margin: 0; }
    .muted { color: #6b7280; font-size: 13px; }
    textarea, input[type="number"] { width: 100%; box-sizing: border-box; border: 1px solid #d1d5db; border-radius: 8px; padding: 10px 12px; font: inherit; }
    textarea { min-height: 110px; resize: vertical; }
    button { border: 0; border-radius: 8px; padding: 10px 14px; font: inherit; cursor: pointer; }
    .primary { background: #2563eb; color: white; }
    .secondary { background: #eef2ff; color: #1e3a8a; }
    .danger { background: #fee2e2; color: #991b1b; }
    .row { display: grid; grid-template-columns: 1fr 140px; gap: 14px; align-items: end; }
    .stack { display: grid; gap: 14px; }
    .toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
    .status { min-height: 20px; }
    .stream { white-space: pre-wrap; word-break: break-word; border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px; background: #fbfbfd; min-height: 240px; }
    .line { padding: 8px 10px; border-bottom: 1px solid #eef2f7; }
    .line:last-child { border-bottom: 0; }
    .kv { display: grid; gap: 4px; }
    .small { font-size: 12px; color: #6b7280; }
    a { color: #2563eb; }
    details { margin-top: 12px; }
    summary { cursor: pointer; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <h1>RAG UI</h1>
        <div class="muted">Streaming generation through the frontend service.</div>
      </div>
      <div id="auth-controls" class="muted"></div>
    </div>

    <div class="card stack">
      <div>
        <label for="query" class="muted">Query</label>
        <textarea id="query" placeholder="Ask a question..."></textarea>
      </div>

      <div class="row">
        __TOPK_CONTROL__
        <div class="toolbar" style="justify-content:flex-end">
          <button id="ask" class="primary" type="button">Ask</button>
          <button id="clear" class="secondary" type="button">Clear</button>
          <button id="logout" class="danger" type="button">Logout</button>
        </div>
      </div>

      <div id="status" class="status muted"></div>

      <div>
        <div class="muted" style="margin-bottom:8px">Stream output</div>
        <div id="stream" class="stream"></div>
      </div>

      __DEBUG_SECTION__
    </div>
  </div>

<script>
const REQUIRE_AUTH = __REQUIRE_AUTH__;
const DISPLAY_TOPK = __DISPLAY_TOPK__;
const TOKEN_KEY = "app_jwt";

const els = {
  auth: document.getElementById("auth-controls"),
  query: document.getElementById("query"),
  topK: document.getElementById("top_k"),
  ask: document.getElementById("ask"),
  clear: document.getElementById("clear"),
  logout: document.getElementById("logout"),
  status: document.getElementById("status"),
  stream: document.getElementById("stream"),
  debug: document.getElementById("debug"),
};

function safeText(value) {
  return String(value ?? "");
}

function escapeHtml(s) {
  return safeText(s).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}

function setStatus(text) {
  els.status.textContent = text || "";
}

function setStream(text) {
  els.stream.textContent = text || "";
}

function setDebug(text) {
  if (els.debug) {
    els.debug.textContent = text || "";
  }
}

function token() {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch (e) {
    return "";
  }
}

function saveToken(value) {
  try {
    if (value) {
      localStorage.setItem(TOKEN_KEY, value);
    } else {
      localStorage.removeItem(TOKEN_KEY);
    }
  } catch (e) {}
}

function authUi(text, signedIn) {
  els.auth.innerHTML = text;
  els.logout.disabled = !signedIn && REQUIRE_AUTH;
  els.ask.disabled = REQUIRE_AUTH && !signedIn;
  els.query.disabled = REQUIRE_AUTH && !signedIn;
  if (DISPLAY_TOPK && els.topK) {
    els.topK.disabled = REQUIRE_AUTH && !signedIn;
  }
}

async function validateAuth() {
  const tok = token();
  if (!tok) {
    authUi(REQUIRE_AUTH ? '<a href="/auth/login">Login</a>' : 'Anonymous mode', false);
    return false;
  }

  try {
    const resp = await fetch('/auth/me', {
      headers: { 'Authorization': 'Bearer ' + tok }
    });
    if (!resp.ok) {
      saveToken("");
      authUi(REQUIRE_AUTH ? '<a href="/auth/login">Login</a>' : 'Anonymous mode', false);
      return false;
    }
    const data = await resp.json();
    const user = data && data.user ? data.user : {};
    const label = user.name || user.email || user.sub || 'signed in';
    authUi('Signed in as ' + escapeHtml(label) + ' · <a href="/auth/logout">Logout</a>', true);
    return true;
  } catch (e) {
    saveToken("");
    authUi(REQUIRE_AUTH ? '<a href="/auth/login">Login</a>' : 'Anonymous mode', false);
    return false;
  }
}

function decodeLine(line) {
  const cleaned = line.trim();
  if (!cleaned) {
    return null;
  }
  const payload = cleaned.startsWith("data:") ? cleaned.slice(5).trimStart() : cleaned;
  if (!payload) {
    return null;
  }
  try {
    return JSON.parse(payload);
  } catch (e) {
    return payload;
  }
}

function renderItem(item) {
  if (typeof item === "string") {
    return '<div class="line">' + escapeHtml(item) + '</div>';
  }
  if (item && typeof item === "object") {
    const kind = item.type || item.event || "item";
    const main = item.text || item.delta || item.answer || item.message || JSON.stringify(item);
    const meta = Object.keys(item)
      .filter(k => !["type", "event", "text", "delta", "answer", "message"].includes(k))
      .map(k => escapeHtml(k) + ": " + escapeHtml(item[k]))
      .join(" · ");
    return '<div class="line"><div class="kv"><div>' + escapeHtml(kind) + '</div><div>' + escapeHtml(main) + '</div><div class="small">' + meta + '</div></div></div>';
  }
  return '';
}

async function streamGeneration() {
  const q = els.query.value.trim();
  if (!q) {
    setStatus('Query required');
    return;
  }

  const signedIn = await validateAuth();
  if (REQUIRE_AUTH && !signedIn) {
    setStatus('Login required');
    window.location.href = '/auth/login';
    return;
  }

  const payload = {
    query: q,
    top_k: DISPLAY_TOPK && els.topK ? Number.parseInt(els.topK.value || '5', 10) : 5
  };

  setStream('');
  setDebug(JSON.stringify({ request: payload }, null, 2));
  setStatus('Streaming...');
  els.ask.disabled = true;
  els.clear.disabled = true;

  const headers = { 'Content-Type': 'application/json' };
  const tok = token();
  if (tok) {
    headers['Authorization'] = 'Bearer ' + tok;
  }

  try {
    const resp = await fetch('/generate/stream', {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      const text = await resp.text();
      setStatus('Error ' + resp.status);
      setStream(text);
      return;
    }

    if (!resp.body) {
      const text = await resp.text();
      setStream(text);
      setStatus('Done');
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let htmlOut = "";

    while (true) {
      const result = await reader.read();
      if (result.done) {
        break;
      }
      buffer += decoder.decode(result.value, { stream: true });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() || "";
      for (const line of lines) {
        const item = decodeLine(line);
        if (item !== null) {
          htmlOut += renderItem(item);
          els.stream.innerHTML = htmlOut || escapeHtml(String(item));
        }
      }
    }

    if (buffer.trim()) {
      const item = decodeLine(buffer);
      if (item !== null) {
        htmlOut += renderItem(item);
      }
    }
    els.stream.innerHTML = htmlOut || escapeHtml(buffer);
    setStatus('Done');
  } catch (e) {
    setStatus('Request failed');
    setStream(String(e));
  } finally {
    els.ask.disabled = REQUIRE_AUTH && !(await validateAuth());
    els.clear.disabled = false;
  }
}

async function doLogout() {
  try {
    await fetch('/auth/logout');
  } catch (e) {}
  saveToken("");
  setStream('');
  setDebug('');
  await validateAuth();
  setStatus('Logged out');
}

function clearUi() {
  els.query.value = '';
  setStream('');
  setDebug('');
  setStatus('');
}

document.addEventListener('DOMContentLoaded', async () => {
  els.ask.addEventListener('click', streamGeneration);
  els.clear.addEventListener('click', clearUi);
  els.logout.addEventListener('click', doLogout);

  els.query.addEventListener('keydown', (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key === 'Enter') {
      ev.preventDefault();
      streamGeneration();
    }
  });

  await validateAuth();
});
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index():
    topk_control = """<div>
          <label for="top_k" class="muted">Top K</label>
          <input id="top_k" type="number" min="1" max="50" value="5">
        </div>""" if DISPLAY_TOPK_IN_UI else "<div></div>"
    debug_section = """<details>
        <summary>Debug</summary>
        <pre id="debug" class="stream" style="min-height:120px"></pre>
      </details>"""
    html_text = _INDEX_TEMPLATE.replace("__REQUIRE_AUTH__", "true" if REQUIRE_AUTH else "false")
    html_text = html_text.replace("__DISPLAY_TOPK__", "true" if DISPLAY_TOPK_IN_UI else "false")
    html_text = html_text.replace("__TOPK_CONTROL__", topk_control)
    html_text = html_text.replace("__DEBUG_SECTION__", debug_section)
    return HTMLResponse(html_text)

@app.get("/health", include_in_schema=False)
async def health():
    return JSONResponse(
        {
            "status": "ok",
            "service": SERVICE_NAME,
            "env": ENV,
            "require_auth": REQUIRE_AUTH,
            "display_topk": DISPLAY_TOPK_IN_UI,
        }
    )