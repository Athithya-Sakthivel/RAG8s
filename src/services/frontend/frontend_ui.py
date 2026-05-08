# frontend_ui.py
from __future__ import annotations

import os

from fastapi import APIRouter, Request
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

INDEX_TEMPLATE = r"""<!doctype html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RAG UI</title>
<link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
</head>
<body class="bg-gray-50 min-h-screen p-6">
<div class="max-w-4xl mx-auto">
  <div class="flex justify-between items-center mb-6">
    <h1 class="text-2xl font-semibold">RAG UI</h1>
    <div id="auth-controls" class="text-sm"></div>
  </div>
  <form id="qry" class="space-y-4 bg-white p-4 rounded shadow" onsubmit="return false;">
    <label class="block text-sm font-medium">Query</label>
    <textarea id="query" rows="3" class="mt-1 block w-full border rounded p-2" placeholder="Ask your question..."></textarea>
    <div class="flex items-center space-x-4">
      {% if display_sources %}
      <label class="flex items-center space-x-2"><input id="enable_tracing" type="checkbox"/><span class="text-sm">Enable tracing</span></label>
      {% endif %}
      {% if display_topk %}
      <label class="text-sm">Top K <input id="top_k" type="number" value="5" min="1" max="50" class="ml-2 w-20 border rounded p-1 text-sm"/></label>
      {% endif %}
    </div>
    <div><button id="ask" type="button" class="bg-blue-600 text-white px-4 py-2 rounded" {% if require_auth %}disabled{% endif %}>{% if require_auth %}Login required{% else %}Ask{% endif %}</button></div>
  </form>
  <div id="result" class="mt-6"></div>
</div>
<script>
const DISPLAY_SOURCES = {{ 'true' if display_sources else 'false' }};
const DISPLAY_TOPK = {{ 'true' if display_topk else 'false' }};
const REQUIRE_AUTH = {{ 'true' if require_auth else 'false' }};

async function checkAuth(){
  const ctrl = document.getElementById('auth-controls');
  const askBtn = document.getElementById('ask');
  const tok = localStorage.getItem('app_jwt');
  if(!tok){
    ctrl.innerHTML = '<a href="/auth/login" class="text-sm text-blue-600 underline">Login</a>';
    setQueryEditable(false);
    return;
  }
  try{
    const resp = await fetch('/auth/me',{headers:{'Authorization':'Bearer '+tok}});
    if(!resp.ok){
      localStorage.removeItem('app_jwt');
      ctrl.innerHTML = '<a href="/auth/login" class="text-sm text-blue-600 underline">Login</a>';
      setQueryEditable(false);
      return;
    }
    const j = await resp.json();
    const name = j.user && (j.user.name || j.user.email || j.user.sub) || 'user';
    ctrl.innerHTML = '<span class="mr-4 text-sm text-gray-700">Signed in as '+escapeHtml(name)+'</span><button id="logout-btn" class="text-sm text-red-600 underline">Logout</button>';
    document.getElementById('logout-btn').addEventListener('click', async function(){ try{ await fetch('/auth/logout'); }catch(e){} localStorage.removeItem('app_jwt'); window.location.reload(); });
    setQueryEditable(true);
  }catch(e){
    localStorage.removeItem('app_jwt');
    ctrl.innerHTML = '<a href="/auth/login" class="text-sm text-blue-600 underline">Login</a>';
    setQueryEditable(false);
  }
}
function setQueryEditable(allow){
  const q = document.getElementById('query');
  const btn = document.getElementById('ask');
  if(q) q.disabled = !allow;
  if(btn){
    btn.disabled = !allow;
    btn.innerText = allow ? 'Ask' : 'Login required';
  }
}
function escapeHtml(s){
  if(!s) return '';
  return s.replace(/[&<>"']/g, function(m){ return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]); });
}
function escapeAttr(s){ return escapeHtml(s).replace(/"/g,'&quot'); }

async function submitStream(){
  const q=document.getElementById('query').value.trim();
  if(!q){ document.getElementById('result').innerHTML='<div class="bg-red-100 p-3 rounded">Query required</div>'; return; }
  let top_k = 5;
  if(DISPLAY_TOPK){
    top_k = parseInt(document.getElementById('top_k').value||'5',10);
  }
  const enable_tracing = DISPLAY_SOURCES ? document.getElementById('enable_tracing').checked===true : false;
  const payload={ query: q, top_k, enable_tracing, return_chunks: enable_tracing };
  document.getElementById('ask').disabled=true; document.getElementById('ask').innerText='Asking...';
  const resultDiv = document.getElementById('result');
  resultDiv.innerHTML = '<div class="bg-white p-4 rounded shadow"><div id="answer-text" class="prose whitespace-pre-wrap"></div></div>';
  const answerEl = document.getElementById('answer-text');

  try{
    const tok = localStorage.getItem('app_jwt');
    const headers = {'Content-Type':'application/json'};
    if(tok){ headers['Authorization'] = 'Bearer '+tok; }
    const resp = await fetch('/generate/stream', { method:'POST', headers: headers, body: JSON.stringify(payload) });
    if(!resp.ok){
      const text = await resp.text();
      resultDiv.innerHTML = '<div class="bg-red-100 p-3 rounded">Error: '+resp.status+' — '+escapeHtml(text)+'</div>';
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let eventType = '';
    let finalChunks = [];
    let buffer = '';
    while(true){
      const { done, value } = await reader.read();
      if(done) break;
      buffer += decoder.decode(value, {stream:true});
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for(const line of lines){
        if(line.startsWith('event: ')){ eventType = line.slice(7).trim(); continue; }
        if(line.startsWith('data: ')){
          try{ var data = JSON.parse(line.slice(6)); }catch(e){ continue; }
          if(eventType === 'delta'){
            answerEl.textContent += data.text;
          } else if(eventType === 'done'){
            finalChunks = data.chunks || [];
            renderResult({ answer: data.answer, chunks: finalChunks });
          }
        }
      }
    }
  } catch(e){
    resultDiv.innerHTML='<div class="bg-red-100 p-3 rounded">Request failed: '+String(e)+'</div>';
  } finally{
    document.getElementById('ask').disabled=false; document.getElementById('ask').innerText='Ask';
  }
}

function renderResult(json){
  const res = document.getElementById('result');
  let out = '<div class="bg-white p-4 rounded shadow"><h2 class="font-medium mb-2">Answer</h2>';
  out += '<div class="prose"><pre class="whitespace-pre-wrap">'+(json.answer||'')+'</pre></div>';
  const chunks = json.chunks || [];
  if(DISPLAY_SOURCES && chunks.length){
    out += '<h3 class="mt-4 font-medium">Sources</h3><ul class="space-y-2">';
    chunks.forEach((c, idx) => {
      out += '<li class="p-2 border rounded"><div class="text-sm w-full">';
      out += '<div class="font-medium">['+ (c.index || (idx+1)) +']</div>';
      out += '<div class="mt-1 text-xs text-gray-700"><ul class="list-none p-0 m-0">';
      const meta = c.meta_items || [];
      meta.forEach(it => {
        if(it.k === 'content'){
          out += '<li><details><summary class="cursor-pointer text-blue-600">Show content</summary><div class="mt-2 text-xs text-gray-800 whitespace-pre-wrap">'+escapeHtml(it.v)+'</div></details></li>';
        } else if(it.k === 'source_url'){
          out += '<li><strong>'+escapeHtml(it.k)+':</strong> <a href="#" class="source-link text-blue-600 underline" data-s3="'+escapeHtml(it.v)+'">open</a></li>';
        } else {
          out += '<li><strong>'+escapeHtml(it.k)+':</strong> '+escapeHtml(String(it.v))+'</li>';
        }
      });
      out += '</ul></div>';
      out += '<div class="mt-2 text-xs text-gray-500 presign-result" id="presign-'+idx+'"></div>';
      out += '</div></li>';
    });
    out += '</ul>';
  }
  out += '</div>';
  res.innerHTML = out;
  document.querySelectorAll('.source-link').forEach((el, i) => {
    el.addEventListener('click', async function(ev){
      ev.preventDefault();
      const s3 = el.getAttribute('data-s3');
      const presignDiv = document.getElementById('presign-'+i);
      presignDiv.textContent = 'Fetching presigned URL...';
      try{
        const tok = localStorage.getItem('app_jwt');
        const headers = {'Content-Type':'application/json'};
        if(tok){ headers['Authorization'] = 'Bearer '+tok; }
        const r = await fetch('/presign', { method:'POST', headers: headers, body: JSON.stringify({ s3_path: s3, expires:3600, inline:true })});
        const j = await r.json();
        if(r.ok && j.url){
          presignDiv.innerHTML = "<a href='"+escapeAttr(j.url)+"' target='_blank' class='text-green-600 underline'>Open presigned URL</a><div class='text-xs text-gray-600 break-words'>"+escapeHtml(j.url)+"</div>";
        } else {
          presignDiv.textContent = 'presign failed: ' + (j.detail || j.error || JSON.stringify(j));
        }
      }catch(e){
        presignDiv.textContent = 'presign error: ' + String(e);
      }
    });
  });
}

document.addEventListener('DOMContentLoaded', function(){
  checkAuth();
  const askBtn = document.getElementById('ask');
  askBtn.onclick = submitStream;
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