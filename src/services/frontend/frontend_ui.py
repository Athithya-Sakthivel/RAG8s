# frontend_ui.py
from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from jinja2 import Environment, BaseLoader, select_autoescape

from config import (
    DISPLAY_SOURCES_IN_UI,
    DISPLAY_TOPK_IN_UI,
    REQUIRE_AUTH,
)

router = APIRouter(tags=["ui"])

DISPLAY_SOURCES = bool(DISPLAY_SOURCES_IN_UI)
DISPLAY_TOPK = bool(DISPLAY_TOPK_IN_UI)
REQUIRE_AUTH_UI = bool(REQUIRE_AUTH)

INDEX_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RAG UI</title>
  <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
  <style>
    .typing-cursor::after {
      content: "▋";
      animation: blink 1s step-end infinite;
    }
    @keyframes blink { 50% { opacity: 0; } }
    .source-link:hover { text-decoration: underline; }
    #answer-text { white-space: pre-wrap; word-break: break-word; }
  </style>
</head>
<body class="bg-gray-50 min-h-screen flex flex-col">
  <header class="bg-white shadow-sm">
    <div class="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
      <h1 class="text-xl font-bold text-gray-800">RAG UI</h1>
      <div id="auth-controls" class="text-sm"></div>
    </div>
  </header>

  <main class="flex-1 max-w-5xl mx-auto px-6 py-8 w-full">
    <form id="qry" class="space-y-4 bg-white p-6 rounded-lg shadow" onsubmit="return false;">
      <label class="block text-sm font-medium text-gray-700">Your Question</label>
      <textarea id="query" rows="3" class="mt-1 block w-full border border-gray-300 rounded-lg p-3 focus:ring-2 focus:ring-blue-500 focus:border-transparent" placeholder="Ask anything..."></textarea>

      <div class="flex items-center space-x-6">
        {% if display_sources %}
        <label class="flex items-center space-x-2 text-sm text-gray-600">
          <input id="enable_tracing" type="checkbox" class="rounded" checked/>
          <span>Show sources</span>
        </label>
        {% endif %}
        {% if display_topk %}
        <label class="text-sm text-gray-600">Top‑K
          <input id="top_k" type="number" value="5" min="1" max="50" class="ml-2 w-20 border border-gray-300 rounded p-1 text-sm"/>
        </label>
        {% endif %}
      </div>

      <button id="ask" type="button" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-medium py-2.5 px-4 rounded-lg transition disabled:opacity-50 disabled:cursor-not-allowed"
              {% if require_auth %}disabled{% endif %}>
        {% if require_auth %}Sign in to ask{% else %}Ask{% endif %}
      </button>
    </form>

    <div id="result" class="mt-6"></div>
  </main>

  <script>
    const DISPLAY_SOURCES = {{ 'true' if display_sources else 'false' }};
    const DISPLAY_TOPK    = {{ 'true' if display_topk else 'false' }};
    const REQUIRE_AUTH    = {{ 'true' if require_auth else 'false' }};

    // ---------- Auth ----------
    async function checkAuth() {
      const ctrl = document.getElementById('auth-controls');
      const askBtn = document.getElementById('ask');
      const tok = localStorage.getItem('app_jwt');

      if (!tok) {
        ctrl.innerHTML = '<a href="/auth/login" class="text-sm text-blue-600 hover:text-blue-800 font-medium">Sign in</a>';
        setQueryEditable(false);
        return;
      }

      try {
        const resp = await fetch('/auth/me', { headers: { 'Authorization': 'Bearer ' + tok } });
        if (!resp.ok) throw new Error('invalid');
        const j = await resp.json();
        const name = j.user && (j.user.name || j.user.email || j.user.sub) || 'user';
        ctrl.innerHTML = `<span class="mr-4 text-sm text-gray-700">${escapeHtml(name)}</span>
          <button id="logout-btn" class="text-sm text-red-600 hover:text-red-800 font-medium">Logout</button>`;
        document.getElementById('logout-btn').addEventListener('click', async () => {
          await fetch('/auth/logout');
          localStorage.removeItem('app_jwt');
          window.location.reload();
        });
        setQueryEditable(true);
      } catch (e) {
        localStorage.removeItem('app_jwt');
        ctrl.innerHTML = '<a href="/auth/login" class="text-sm text-blue-600 hover:text-blue-800 font-medium">Sign in</a>';
        setQueryEditable(false);
      }
    }

    function setQueryEditable(allow) {
      const q = document.getElementById('query');
      const btn = document.getElementById('ask');
      if (q) q.disabled = !allow;
      if (btn) {
        btn.disabled = !allow;
        btn.innerText = allow ? 'Ask' : 'Sign in to ask';
      }
    }

    // ---------- Utilities ----------
    function escapeHtml(s) {
      if (!s) return '';
      const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
      return s.replace(/[&<>"']/g, m => map[m]);
    }
    function escapeAttr(s) { return escapeHtml(s).replace(/"/g, '&quot;'); }

    // ---------- Streaming RAG ----------
    async function submitStream() {
      const q = document.getElementById('query').value.trim();
      if (!q) return;

      let top_k = 5;
      if (DISPLAY_TOPK) {
        top_k = parseInt(document.getElementById('top_k').value || '5', 10);
      }
      const returnChunks = DISPLAY_SOURCES && document.getElementById('enable_tracing')?.checked;

      const payload = { query: q, top_k, return_chunks: returnChunks };

      const askBtn = document.getElementById('ask');
      askBtn.disabled = true;
      askBtn.innerText = 'Asking...';

      const resultDiv = document.getElementById('result');
      resultDiv.innerHTML = `
        <div class="bg-white p-6 rounded-lg shadow">
          <div id="answer-text" class="text-gray-800 leading-relaxed typing-cursor"></div>
        </div>`;
      const answerEl = document.getElementById('answer-text');

      try {
        const tok = localStorage.getItem('app_jwt');
        const headers = { 'Content-Type': 'application/json' };
        if (tok) headers['Authorization'] = 'Bearer ' + tok;

        const resp = await fetch('/generate/stream', {
          method: 'POST',
          headers,
          body: JSON.stringify(payload)
        });

        if (!resp.ok) {
          const text = await resp.text();
          resultDiv.innerHTML = `<div class="bg-red-100 border border-red-300 text-red-700 p-4 rounded-lg">Error ${resp.status}: ${escapeHtml(text)}</div>`;
          return;
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let eventType = '';
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';
          for (const line of lines) {
            if (line.startsWith('event: ')) {
              eventType = line.slice(7).trim();
              continue;
            }
            if (line.startsWith('data: ')) {
              let data;
              try { data = JSON.parse(line.slice(6)); } catch (e) { continue; }
              if (eventType === 'delta') {
                answerEl.textContent += data.text;
              } else if (eventType === 'done') {
                answerEl.classList.remove('typing-cursor');
                renderResult({ answer: data.answer, chunks: data.chunks || [] });
              }
            }
          }
        }
      } catch (e) {
        resultDiv.innerHTML = `<div class="bg-red-100 border border-red-300 text-red-700 p-4 rounded-lg">Request failed: ${escapeHtml(String(e))}</div>`;
      } finally {
        askBtn.disabled = false;
        askBtn.innerText = 'Ask';
      }
    }

    // ---------- Render final answer + citations ----------
    function renderResult(json) {
      const resultDiv = document.getElementById('result');
      let html = `<div class="bg-white p-6 rounded-lg shadow">
        <h2 class="text-lg font-semibold mb-4 text-gray-800">Answer</h2>
        <div class="prose max-w-none mb-6"><pre class="whitespace-pre-wrap text-gray-700" id="answer-text-final">${escapeHtml(json.answer || '')}</pre></div>`;

      const chunks = json.chunks || [];
      if (DISPLAY_SOURCES && chunks.length) {
        html += `<h2 class="text-lg font-semibold mb-3 text-gray-800">Sources</h2><div class="space-y-4">`;
        chunks.forEach((c, idx) => {
          const meta = c.meta_items || [];
          html += `<div class="border border-gray-200 rounded-lg p-4">
            <div class="font-medium text-gray-700 mb-2">[${c.index || (idx + 1)}]</div>
            <ul class="text-sm text-gray-600 space-y-1">`;
          meta.forEach(it => {
            if (it.k === 'content') {
              html += `<li>
                <details class="group">
                  <summary class="cursor-pointer text-blue-600 hover:text-blue-800 font-medium">Show content</summary>
                  <div class="mt-2 p-2 bg-gray-50 rounded text-xs whitespace-pre-wrap max-h-48 overflow-y-auto">${escapeHtml(it.v)}</div>
                </details>
              </li>`;
            } else if (it.k === 'source_url') {
              html += `<li><span class="font-medium">source:</span>
                <a href="#" class="source-link text-blue-600 hover:text-blue-800 ml-1" data-s3="${escapeAttr(it.v)}">open</a>
              </li>`;
            } else {
              html += `<li><span class="font-medium">${escapeHtml(it.k)}:</span> ${escapeHtml(String(it.v))}</li>`;
            }
          });
          html += `</ul>
            <div class="mt-3 text-xs text-gray-500 presign-result" id="presign-${idx}"></div>
          </div>`;
        });
        html += `</div>`;
      }
      html += `</div>`;
      resultDiv.innerHTML = html;

      // Attach presigned URL handlers
      document.querySelectorAll('.source-link').forEach((el, i) => {
        el.addEventListener('click', async (ev) => {
          ev.preventDefault();
          const s3 = el.getAttribute('data-s3');
          const presignDiv = document.getElementById('presign-' + i);
          presignDiv.textContent = 'Fetching secure link...';
          try {
            const tok = localStorage.getItem('app_jwt');
            const headers = { 'Content-Type': 'application/json' };
            if (tok) headers['Authorization'] = 'Bearer ' + tok;
            const resp = await fetch('/presign', {
              method: 'POST',
              headers,
              body: JSON.stringify({ s3_path: s3 })
            });
            const j = await resp.json();
            if (resp.ok && j.url) {
              presignDiv.innerHTML = `<a href="${escapeAttr(j.url)}" target="_blank" rel="noopener" class="text-green-600 hover:text-green-800 font-medium underline">Open document</a>
                <div class="text-xs text-gray-400 mt-1 truncate max-w-xs">${escapeHtml(j.url)}</div>`;
            } else {
              presignDiv.textContent = 'Presigned URL failed: ' + (j.detail || j.error || JSON.stringify(j));
            }
          } catch (e) {
            presignDiv.textContent = 'Error: ' + String(e);
          }
        });
      });
    }

    // ---------- Startup ----------
    document.addEventListener('DOMContentLoaded', () => {
      checkAuth();
      const askBtn = document.getElementById('ask');
      askBtn.addEventListener('click', submitStream);
    });
  </script>
</body>
</html>
"""

env = Environment(loader=BaseLoader(), autoescape=select_autoescape(["html"]))
tmpl = env.from_string(INDEX_TEMPLATE)
INDEX_HTML = tmpl.render(
    display_sources=DISPLAY_SOURCES,
    display_topk=DISPLAY_TOPK,
    require_auth=REQUIRE_AUTH_UI,
)


@router.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)