# frontend_ui.py
from __future__ import annotations

import os

from config import DISPLAY_SOURCES_IN_UI, DISPLAY_TOPK_IN_UI, REQUIRE_AUTH
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
    :root {
      color-scheme: light dark;
      --bg: #f6f7fb;
      --card-bg: #ffffff;
      --text: #111827;
      --muted: #6b7280;
      --border: #e5e7eb;
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --primary-light: #eef2ff;
      --danger: #dc2626;
      --danger-light: #fef2f2;
      --success: #059669;
      --warning: #d97706;
      --shadow: 0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.06);
      --radius: 12px;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0f172a;
        --card-bg: #1e293b;
        --text: #f1f5f9;
        --muted: #94a3b8;
        --border: #334155;
        --primary: #3b82f6;
        --primary-hover: #2563eb;
        --primary-light: #1e3a5f;
        --danger-light: #3b1818;
        --shadow: 0 1px 3px rgba(0,0,0,.3);
      }
    }
    * { box-sizing: border-box; }
    body {
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      margin: 0;
      background: var(--bg);
      color: var(--text);
      line-height: 1.6;
    }
    .wrap { max-width: 1100px; margin: 0 auto; padding: 24px 16px; }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 24px;
      box-shadow: var(--shadow);
    }
    .top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin-bottom: 20px;
      flex-wrap: wrap;
    }
    h1 { font-size: 24px; font-weight: 700; margin: 0; }
    .badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 99px;
      font-size: 11px;
      font-weight: 600;
      background: var(--primary-light);
      color: var(--primary);
    }
    .muted { color: var(--muted); font-size: 13px; }
    label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; color: var(--muted); }
    textarea, input[type="number"] {
      width: 100%;
      box-sizing: border-box;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 14px;
      font: inherit;
      color: var(--text);
      background: var(--card-bg);
      transition: border-color .15s, box-shadow .15s;
    }
    textarea:focus, input:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(37,99,235,.15); }
    textarea:disabled, input:disabled { opacity: .5; cursor: not-allowed; }
    textarea { min-height: 120px; resize: vertical; }
    button {
      border: 0;
      border-radius: 8px;
      padding: 10px 18px;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
      transition: all .15s;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    button:disabled { opacity: .5; cursor: not-allowed; }
    .primary { background: var(--primary); color: white; }
    .primary:hover:not(:disabled) { background: var(--primary-hover); }
    .secondary { background: var(--primary-light); color: var(--primary); }
    .secondary:hover:not(:disabled) { background: #dbeafe; }
    .danger { background: var(--danger-light); color: var(--danger); }
    .danger:hover:not(:disabled) { background: #fee2e2; }
    .row { display: grid; grid-template-columns: 1fr auto; gap: 14px; align-items: end; }
    .stack { display: grid; gap: 16px; }
    .toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
    .status-bar {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 24px;
      font-size: 13px;
    }
    .status-dot {
      width: 8px; height: 8px;
      border-radius: 50%;
      display: inline-block;
    }
    .dot-idle { background: var(--muted); }
    .dot-streaming { background: var(--primary); animation: pulse 1.5s ease-in-out infinite; }
    .dot-done { background: var(--success); }
    .dot-error { background: var(--danger); }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
    .stream {
      white-space: pre-wrap;
      word-break: break-word;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px;
      background: var(--bg);
      min-height: 260px;
      max-height: 600px;
      overflow-y: auto;
      font-size: 14px;
    }
    .chunk {
      padding: 12px 0;
      border-bottom: 1px solid var(--border);
      animation: fadeIn .25s ease;
    }
    .chunk:last-child { border-bottom: 0; }
    @keyframes fadeIn { from{opacity:0;transform:translateY(4px)} to{opacity:1;transform:translateY(0)} }
    .chunk-meta {
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 4px;
    }
    .chunk-content { line-height: 1.7; }
    .citation { color: var(--primary); font-weight: 600; }
    .sources { margin-top: 12px; }
    .source-link { display: block; font-size: 12px; color: var(--primary); word-break: break-all; }
    details { margin-top: 16px; }
    summary { cursor: pointer; color: var(--muted); font-size: 13px; }
    pre { font-size: 12px; white-space: pre-wrap; word-break: break-all; }
    .empty-state {
      text-align: center;
      color: var(--muted);
      padding: 40px 20px;
    }
    .empty-state svg { width: 48px; height: 48px; margin-bottom: 12px; opacity: .4; }
    @media (max-width: 640px) {
      .row { grid-template-columns: 1fr; }
      .top { flex-direction: column; align-items: flex-start; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <h1>RAG UI</h1>
        <div class="muted" style="margin-top:2px">Ask questions, get cited answers from your documents.</div>
      </div>
      <div id="auth-controls"></div>
    </div>

    <div class="card stack">
      <div>
        <label for="query">Question</label>
        <textarea id="query" placeholder="What would you like to know?"></textarea>
      </div>

      <div class="row">
        __TOPK_CONTROL__
        <div class="toolbar" style="justify-content:flex-end">
          <button id="ask" class="primary" type="button">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
            Ask
          </button>
          <button id="clear" class="secondary" type="button">Clear</button>
          <button id="logout" class="danger" type="button" style="display:none">Logout</button>
        </div>
      </div>

      <div class="status-bar">
        <span id="status-dot" class="status-dot dot-idle"></span>
        <span id="status-text" class="muted">Ready</span>
      </div>

      <div>
        <div class="muted" style="margin-bottom:8px;display:flex;justify-content:space-between">
          <span>Response</span>
          <span id="stream-stats" style="font-size:11px"></span>
        </div>
        <div id="stream" class="stream">
          <div class="empty-state">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
            <div>Your answer will appear here</div>
          </div>
        </div>
      </div>

      __DEBUG_SECTION__
    </div>
  </div>

<script>
const REQUIRE_AUTH = __REQUIRE_AUTH__;
const DISPLAY_TOPK = __DISPLAY_TOPK__;
const DISPLAY_SOURCES = __DISPLAY_SOURCES__;
const TOKEN_KEY = "app_jwt";

const els = {
  auth: document.getElementById("auth-controls"),
  query: document.getElementById("query"),
  topK: document.getElementById("top_k"),
  ask: document.getElementById("ask"),
  clear: document.getElementById("clear"),
  logout: document.getElementById("logout"),
  statusDot: document.getElementById("status-dot"),
  statusText: document.getElementById("status-text"),
  stream: document.getElementById("stream"),
  streamStats: document.getElementById("stream-stats"),
  debug: document.getElementById("debug"),
};

function safeText(value) {
  return String(value ?? "");
}

function escapeHtml(s) {
  return safeText(s).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}

function setStatus(text, type) {
  const t = type || "idle";
  els.statusText.textContent = text || "Ready";
  els.statusDot.className = "status-dot dot-" + t;
}

function setStream(html) {
  els.stream.innerHTML = html || '<div class="empty-state"><div>Your answer will appear here</div></div>';
}

function setStats(text) {
  els.streamStats.textContent = text || "";
}

function setDebug(text) {
  if (els.debug) {
    els.debug.textContent = text || "";
  }
}

function token() {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
}

function saveToken(value) {
  try {
    if (value) localStorage.setItem(TOKEN_KEY, value);
    else localStorage.removeItem(TOKEN_KEY);
  } catch (e) {}
}

function authUi(text, signedIn) {
  els.auth.innerHTML = text;
  els.logout.style.display = signedIn ? "inline-flex" : "none";
  els.ask.disabled = REQUIRE_AUTH && !signedIn;
  els.query.disabled = REQUIRE_AUTH && !signedIn;
  if (DISPLAY_TOPK && els.topK) {
    els.topK.disabled = REQUIRE_AUTH && !signedIn;
  }
}

async function validateAuth() {
  const tok = token();
  if (!tok) {
    authUi(REQUIRE_AUTH ? '<a href="/auth/login" style="color:var(--primary)">Sign in</a>' : '<span class="badge">Public</span>', false);
    return false;
  }

  try {
    const resp = await fetch('/auth/me', { headers: { 'Authorization': 'Bearer ' + tok } });
    if (!resp.ok) {
      saveToken("");
      authUi(REQUIRE_AUTH ? '<a href="/auth/login" style="color:var(--primary)">Sign in</a>' : '<span class="badge">Public</span>', false);
      return false;
    }
    const data = await resp.json();
    const user = data && data.user ? data.user : {};
    const label = user.name || user.email || user.sub || 'User';
    authUi('<span class="badge">' + escapeHtml(label) + '</span>', true);
    return true;
  } catch (e) {
    saveToken("");
    authUi(REQUIRE_AUTH ? '<a href="/auth/login" style="color:var(--primary)">Sign in</a>' : '<span class="badge">Public</span>', false);
    return false;
  }
}

function decodeLine(line) {
  const cleaned = line.trim();
  if (!cleaned) return null;
  const payload = cleaned.startsWith("data:") ? cleaned.slice(5).trimStart() : cleaned;
  if (!payload) return null;
  try { return JSON.parse(payload); } catch (e) { return payload; }
}

function renderAnswer(text) {
  return escapeHtml(text)
    .replace(/\[(\d+)\]/g, '<span class="citation">[$1]</span>');
}

function renderChunk(chunk, index) {
  if (!chunk || !chunk.content) return "";
  const source = chunk.source_url || "";
  const page = chunk.page_number ? " · p." + chunk.page_number : "";
  const heading = chunk.heading_path ? " · " + escapeHtml(chunk.heading_path) : "";
  return '<div class="chunk">' +
    '<div class="chunk-meta">Source ' + (index + 1) + page + heading +
    (source ? ' · <a href="' + escapeHtml(source) + '" target="_blank" class="source-link">' + escapeHtml(source) + '</a>' : '') +
    '</div>' +
    '<div class="chunk-content">' + renderAnswer(chunk.content) + '</div>' +
    '</div>';
}

async function streamGeneration() {
  const q = els.query.value.trim();
  if (!q) {
    setStatus("Please enter a question", "error");
    return;
  }

  const signedIn = await validateAuth();
  if (REQUIRE_AUTH && !signedIn) {
    setStatus("Sign in required — redirecting…", "error");
    setTimeout(() => { window.location.href = '/auth/login'; }, 500);
    return;
  }

  const topK = DISPLAY_TOPK && els.topK ? Number.parseInt(els.topK.value || '5', 10) : 5;
  const payload = { query: q, top_k: topK, return_chunks: true };

  setStream('<div class="empty-state"><div>Thinking…</div></div>');
  setDebug(JSON.stringify({ request: payload }, null, 2));
  setStatus("Streaming…", "streaming");
  setStats("");
  els.ask.disabled = true;
  els.clear.disabled = true;

  const headers = { 'Content-Type': 'application/json' };
  const tok = token();
  if (tok) headers['Authorization'] = 'Bearer ' + tok;

  try {
    const resp = await fetch('/generate/stream', {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      const text = await resp.text();
      setStatus("Error " + resp.status, "error");
      setStream('<div class="empty-state"><div>Error ' + resp.status + ': ' + escapeHtml(text) + '</div></div>');
      return;
    }

    if (!resp.body) {
      setStream('<div class="empty-state"><div>Empty response</div></div>');
      setStatus("Done", "done");
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let answerText = "";
    let chunkCount = 0;
    let sources = [];
    let streamingStarted = false;

    while (true) {
      const result = await reader.read();
      if (result.done) break;
      buffer += decoder.decode(result.value, { stream: true });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() || "";

      for (const line of lines) {
        const item = decodeLine(line);
        if (item === null) continue;

        // Handle SSE events
        if (item.text !== undefined) {
          answerText += item.text;
          if (!streamingStarted) {
            setStream('<div class="chunk"><div class="chunk-content">' + renderAnswer(answerText) + '</div></div>');
            streamingStarted = true;
          } else {
            setStream('<div class="chunk"><div class="chunk-content">' + renderAnswer(answerText) + '</div></div>');
          }
        }

        if (item.answer) {
          answerText = item.answer;
          setStream('<div class="chunk"><div class="chunk-content">' + renderAnswer(answerText) + '</div></div>');
        }

        if (item.chunks && Array.isArray(item.chunks)) {
          chunkCount = item.chunks.length;
          sources = item.chunks;
        }
      }
    }

    // Final render with sources
    let html = '<div class="chunk"><div class="chunk-content">' + renderAnswer(answerText) + '</div></div>';
    if (sources.length > 0 && DISPLAY_SOURCES) {
      html += '<details class="sources"><summary>Sources (' + sources.length + ')</summary>';
      sources.forEach((s, i) => { html += renderChunk(s, i); });
      html += '</details>';
    }
    setStream(html);
    setStats(chunkCount + " chunks retrieved");
    setStatus("Done", "done");

  } catch (e) {
    setStatus("Connection failed", "error");
    setStream('<div class="empty-state"><div>Connection failed: ' + escapeHtml(String(e)) + '</div></div>');
  } finally {
    els.ask.disabled = false;
    els.clear.disabled = false;
  }
}

async function doLogout() {
  try { await fetch('/auth/logout'); } catch (e) {}
  saveToken("");
  setStream('<div class="empty-state"><div>Your answer will appear here</div></div>');
  setDebug('');
  setStatus("Signed out", "idle");
  setStats("");
  await validateAuth();
}

function clearUi() {
  els.query.value = '';
  setStream('<div class="empty-state"><div>Your answer will appear here</div></div>');
  setDebug('');
  setStatus("Ready", "idle");
  setStats("");
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

  // Load last query from sessionStorage
  try {
    const saved = sessionStorage.getItem("rag_last_query");
    if (saved) els.query.value = saved;
  } catch(e) {}

  els.query.addEventListener('input', () => {
    try { sessionStorage.setItem("rag_last_query", els.query.value); } catch(e) {}
  });

  await validateAuth();
});
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index():
    topk_control = (
        """<div>
          <label for="top_k">Top K</label>
          <input id="top_k" type="number" min="1" max="50" value="5">
        </div>"""
        if DISPLAY_TOPK_IN_UI
        else "<div></div>"
    )
    debug_section = """<details>
        <summary>Debug</summary>
        <pre id="debug" class="stream" style="min-height:120px"></pre>
      </details>"""

    html_text = _INDEX_TEMPLATE.replace("__REQUIRE_AUTH__", "true" if REQUIRE_AUTH else "false")
    html_text = html_text.replace("__DISPLAY_TOPK__", "true" if DISPLAY_TOPK_IN_UI else "false")
    html_text = html_text.replace("__DISPLAY_SOURCES__", "true" if DISPLAY_SOURCES_IN_UI else "false")
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
            "display_sources": DISPLAY_SOURCES_IN_UI,
        }
    )