(() => {
  const form = document.getElementById("query-form");
  const result = document.getElementById("result");
  const loading = document.getElementById("loading");
  const askBtn = document.getElementById("ask-btn");
  const queryEl = document.getElementById("query");
  const returnChunksEl = document.getElementById("return_chunks");
  const topKEl = document.getElementById("top_k");
  const fetchKEl = document.getElementById("fetch_k");

  let activeAbortController = null;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (m) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }[m]));
  }

  function redirectToLogin() {
    const rd = encodeURIComponent(window.location.href);
    window.location.assign(`/auth/login?rd=${rd}`);
  }

  function setBusy(busy) {
    askBtn.disabled = busy;
    queryEl.disabled = busy;
    returnChunksEl.disabled = busy;
    topKEl.disabled = busy;
    fetchKEl.disabled = busy;
    loading.style.display = busy ? "block" : "none";
    askBtn.textContent = busy ? "Streaming..." : "Ask";
  }

  function renderShell() {
    result.innerHTML = `
      <div class="answer-card">
        <div class="answer-box">
          <div class="answer-head">
            <h2 class="answer-title">Answer</h2>
            <div id="summary" class="summary"></div>
          </div>
          <pre id="answer-text" class="answer-text"></pre>
        </div>
        <div id="sources"></div>
      </div>
    `;
  }

  function renderSummary(summary) {
    const target = document.getElementById("summary");
    if (!target) return;

    const parts = [];
    if (summary.model_version) parts.push(`<span class="pill">model ${escapeHtml(summary.model_version)}</span>`);
    if (summary.retrieval_mode) parts.push(`<span class="pill">mode ${escapeHtml(summary.retrieval_mode)}</span>`);
    if (typeof summary.cache_hit === "boolean") {
      parts.push(`<span class="pill ${summary.cache_hit ? "pill-ok" : "pill-neutral"}">cache ${summary.cache_hit ? "hit" : "miss"}</span>`);
    }
    if (typeof summary.hybrid_capable === "boolean") {
      parts.push(`<span class="pill ${summary.hybrid_capable ? "pill-ok" : "pill-neutral"}">hybrid ${summary.hybrid_capable ? "on" : "off"}</span>`);
    }
    target.innerHTML = parts.join("");
  }

  function normalizeMetaItems(chunk) {
    if (!chunk || typeof chunk !== "object") return [];

    if (Array.isArray(chunk.meta_items)) return chunk.meta_items;
    if (Array.isArray(chunk.meta)) {
      return chunk.meta.map((item) => {
        if (item && typeof item === "object" && "k" in item) return item;
        return { k: "meta", v: item };
      });
    }

    const out = [];
    for (const [k, v] of Object.entries(chunk)) {
      if (["index", "rank", "meta_items", "meta"].includes(k)) continue;
      out.push({ k, v });
    }
    return out;
  }

  function renderSources(chunks) {
    const sources = document.getElementById("sources");
    if (!sources) return;

    if (!Array.isArray(chunks) || chunks.length === 0) {
      sources.innerHTML = "";
      return;
    }

    let html = `<h3 class="sources-title">Sources</h3><div class="sources-list">`;

    for (let i = 0; i < chunks.length; i += 1) {
      const c = chunks[i] || {};
      const index = c.index ?? c.rank ?? (i + 1);
      const meta = normalizeMetaItems(c);

      html += `
        <article class="source-item">
          <div class="source-head">
            <div class="source-index">[${escapeHtml(index)}]</div>
          </div>
          <div class="source-meta">
            ${meta.map((item) => {
              if (!item || typeof item !== "object") return "";
              const key = escapeHtml(item.k ?? "");
              const value = item.v ?? "";

              if (item.k === "content" || item.k === "text" || item.k === "snippet") {
                return `
                  <details>
                    <summary>content</summary>
                    <div class="source-content">${escapeHtml(value)}</div>
                  </details>
                `;
              }

              if (item.k === "source_url" || item.k === "url") {
                return `
                  <div><strong>${key}:</strong> <a href="${escapeHtml(value)}" target="_blank" rel="noreferrer noopener" class="source-link">${escapeHtml(value)}</a></div>
                `;
              }

              return `<div><strong>${key}:</strong> ${escapeHtml(value)}</div>`;
            }).join("")}
          </div>
        </article>
      `;
    }

    html += `</div>`;
    sources.innerHTML = html;
  }

  async function readNdjson(response, onRecord) {
    if (!response.body) throw new Error("Streaming response body unavailable");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      let nlIndex = buffer.indexOf("\n");
      while (nlIndex >= 0) {
        const line = buffer.slice(0, nlIndex).trim();
        buffer = buffer.slice(nlIndex + 1);

        if (line) {
          try {
            onRecord(JSON.parse(line));
          } catch {
            // ignore malformed partial line
          }
        }

        nlIndex = buffer.indexOf("\n");
      }
    }

    const tail = buffer.trim();
    if (tail) onRecord(JSON.parse(tail));
  }

  async function submit(event) {
    event.preventDefault();

    const query = queryEl.value.trim();
    if (!query) return;

    const top_k = Math.max(1, Math.min(50, parseInt(topKEl.value || "5", 10)));
    const fetch_k = Math.max(1, Math.min(200, parseInt(fetchKEl.value || "20", 10)));
    const return_chunks = returnChunksEl.checked === true;

    if (activeAbortController) {
      activeAbortController.abort();
    }
    activeAbortController = new AbortController();

    renderShell();
    setBusy(true);

    const answerText = document.getElementById("answer-text");

    try {
      const response = await fetch("/stream", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        signal: activeAbortController.signal,
        body: JSON.stringify({
          query,
          top_k,
          fetch_k,
          return_chunks,
          allow_semantic_cache: true,
          max_tokens: 400
        }),
        credentials: "same-origin"
      });

      if (response.redirected || (response.url && response.url.includes("auth.athithya.site"))) {
        redirectToLogin();
        return;
      }

      if (response.status === 401 || response.status === 403) {
        redirectToLogin();
        return;
      }

      const contentType = response.headers.get("content-type") || "";
      if (!response.ok) {
        const text = await response.text();
        result.innerHTML = `<div class="error-box">Error ${response.status}: ${escapeHtml(text)}</div>`;
        return;
      }

      if (contentType.includes("text/html")) {
        redirectToLogin();
        return;
      }

      let finalChunks = [];
      let summary = {};

      await readNdjson(response, (msg) => {
        if (!msg || typeof msg !== "object") return;

        if (msg.type === "start") {
          summary = {
            model_version: msg.model_version || summary.model_version,
            retrieval_mode: msg.retrieval_mode || summary.retrieval_mode,
            cache_hit: msg.cache_hit ?? summary.cache_hit,
            hybrid_capable: msg.hybrid_capable ?? summary.hybrid_capable
          };

          if (Array.isArray(msg.chunks) && msg.chunks.length) {
            finalChunks = msg.chunks;
          }
          renderSummary(summary);
          return;
        }

        if (msg.type === "token" || msg.type === "delta") {
          answerText.textContent += msg.text || "";
          return;
        }

        if (msg.type === "final") {
          if (typeof msg.answer === "string" && msg.answer) {
            answerText.textContent = msg.answer;
          }
          if (Array.isArray(msg.chunks)) {
            finalChunks = msg.chunks;
          }

          summary = {
            model_version: msg.model_version || summary.model_version,
            retrieval_mode: msg.retrieval_mode || summary.retrieval_mode,
            cache_hit: msg.cache_hit ?? summary.cache_hit,
            hybrid_capable: msg.hybrid_capable ?? summary.hybrid_capable
          };

          renderSummary(summary);
          renderSources(finalChunks);
          return;
        }

        if (msg.type === "error") {
          answerText.textContent += `\n\nError: ${msg.error || "stream failed"}`;
          return;
        }

        if (msg.type === "end") {
          if (Array.isArray(msg.chunks)) {
            finalChunks = msg.chunks;
            renderSources(finalChunks);
          }
        }
      });

      if (finalChunks.length) renderSources(finalChunks);
    } catch (err) {
      if (String(err?.name || "") !== "AbortError") {
        result.innerHTML = `<div class="error-box">Request failed: ${escapeHtml(err)}</div>`;
      }
    } finally {
      activeAbortController = null;
      setBusy(false);
    }
  }

  form.addEventListener("submit", submit);
})();