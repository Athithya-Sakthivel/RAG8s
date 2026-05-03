(() => {
  const form = document.getElementById("query-form");
  const result = document.getElementById("result");
  const loading = document.getElementById("loading");
  const askBtn = document.getElementById("ask-btn");
  const queryEl = document.getElementById("query");
  const returnChunksEl = document.getElementById("return_chunks");
  const topKEl = document.getElementById("top_k");
  const fetchKEl = document.getElementById("fetch_k");
  const maxTokensEl = document.getElementById("max_tokens");

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

  function setBusy(busy) {
    askBtn.disabled = busy;
    queryEl.disabled = busy;
    returnChunksEl.disabled = busy;
    topKEl.disabled = busy;
    fetchKEl.disabled = busy;
    maxTokensEl.disabled = busy;
    loading.style.display = busy ? "block" : "none";
    askBtn.textContent = busy ? "Streaming..." : "Ask";
  }

  function redirectToConsole() {
    window.location.assign("https://auth.athithya.site/ui/console/");
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

    if (summary.cache_hit === true) {
      parts.push(`<span class="pill pill-ok">cache hit</span>`);
    } else if (summary.cache_hit === false) {
      parts.push(`<span class="pill pill-neutral">cache miss</span>`);
    }

    if (typeof summary.cache_score === "number") {
      parts.push(`<span class="pill">cache ${escapeHtml(summary.cache_score.toFixed(3))}</span>`);
    }

    if (summary.retrieval_mode) {
      parts.push(`<span class="pill">mode ${escapeHtml(summary.retrieval_mode)}</span>`);
    }

    if (typeof summary.hybrid_capable === "boolean") {
      parts.push(
        `<span class="pill ${summary.hybrid_capable ? "pill-ok" : "pill-neutral"}">
          hybrid ${summary.hybrid_capable ? "on" : "off"}
        </span>`
      );
    }

    target.innerHTML = parts.join(" ");
  }

  function normalizeMetaItems(chunk) {
    if (!chunk || typeof chunk !== "object") return [];

    const skip = new Set([
      "index", "rank", "meta_items", "meta",
      "content", "text", "snippet", "answer"
    ]);

    if (Array.isArray(chunk.meta_items)) {
      return chunk.meta_items;
    }

    if (Array.isArray(chunk.meta)) {
      return chunk.meta.map((item) => {
        if (item && typeof item === "object" && "k" in item) return item;
        return { k: "meta", v: item };
      });
    }

    const out = [];
    for (const [k, v] of Object.entries(chunk)) {
      if (skip.has(k)) continue;
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

      const content = c.content ?? c.text ?? c.snippet ?? "";

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

              if (item.k === "source_url" || item.k === "url") {
                return `
                  <div>
                    <strong>${key}:</strong>
                    <a class="source-link" href="${escapeHtml(value)}" target="_blank" rel="noreferrer noopener">
                      ${escapeHtml(value)}
                    </a>
                  </div>
                `;
              }

              return `<div><strong>${key}:</strong> ${escapeHtml(value)}</div>`;
            }).join("")}

            ${content ? `
              <details>
                <summary>content</summary>
                <div class="source-content">${escapeHtml(content)}</div>
              </details>
            ` : ""}
          </div>
        </article>
      `;
    }

    html += `</div>`;
    sources.innerHTML = html;
  }

  async function readSSE(response, onEvent) {
    if (!response.body) throw new Error("Streaming response body unavailable");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    function emitFrame(frame) {
      if (!frame.trim()) return;

      let eventName = "message";
      const dataLines = [];

      for (const rawLine of frame.split(/\r?\n/)) {
        if (!rawLine) continue;
        if (rawLine.startsWith("event:")) {
          eventName = rawLine.slice(6).trim();
        } else if (rawLine.startsWith("data:")) {
          dataLines.push(rawLine.slice(5).replace(/^ /, ""));
        }
      }

      const dataText = dataLines.join("\n").trim();
      if (!dataText) return;

      let data;
      try {
        data = JSON.parse(dataText);
      } catch {
        data = { raw: dataText };
      }

      onEvent(eventName, data);
    }

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      let sepIndex = buffer.indexOf("\n\n");
      while (sepIndex >= 0) {
        const frame = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        emitFrame(frame);
        sepIndex = buffer.indexOf("\n\n");
      }
    }

    const tail = buffer.trim();
    if (tail) {
      emitFrame(tail);
    }
  }

  async function submit(event) {
    event.preventDefault();

    const query = queryEl.value.trim();
    if (!query) return;

    const top_k = Math.max(1, Math.min(50, parseInt(topKEl.value || "5", 10)));
    const fetch_k = Math.max(1, Math.min(200, parseInt(fetchKEl.value || "20", 10)));
    const max_tokens = Math.max(64, Math.min(4096, parseInt(maxTokensEl.value || "400", 10)));
    const return_chunks = returnChunksEl.checked === true;

    if (activeAbortController) {
      activeAbortController.abort();
    }
    activeAbortController = new AbortController();

    renderShell();
    setBusy(true);

    const answerText = document.getElementById("answer-text");

    try {
      const response = await fetch("/generate/stream", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        signal: activeAbortController.signal,
        credentials: "same-origin",
        body: JSON.stringify({
          query,
          top_k,
          fetch_k,
          return_chunks,
          allow_semantic_cache: true,
          max_tokens
        })
      });

      if (response.redirected || String(response.url || "").includes("auth.athithya.site")) {
        redirectToConsole();
        return;
      }

      if (response.status === 401 || response.status === 403) {
        redirectToConsole();
        return;
      }

      const contentType = response.headers.get("content-type") || "";
      if (!response.ok) {
        const text = await response.text();
        result.innerHTML = `<div class="error-box">Error ${response.status}: ${escapeHtml(text)}</div>`;
        return;
      }

      if (!contentType.includes("text/event-stream")) {
        const text = await response.text();
        if (text.includes("auth.athithya.site") || text.includes("<html")) {
          redirectToConsole();
          return;
        }
        result.innerHTML = `<div class="error-box">Unexpected response: ${escapeHtml(text)}</div>`;
        return;
      }

      let finalChunks = [];
      let summary = {
        cache_hit: null,
        cache_score: null,
        retrieval_mode: null,
        hybrid_capable: null
      };

      await readSSE(response, (eventName, msg) => {
        if (!msg || typeof msg !== "object") return;

        if (eventName === "start") {
          summary = {
            cache_hit: msg.cache_hit ?? summary.cache_hit,
            cache_score: msg.cache_score ?? summary.cache_score,
            retrieval_mode: msg.retrieval_mode ?? summary.retrieval_mode,
            hybrid_capable: msg.hybrid_capable ?? summary.hybrid_capable
          };

          if (Array.isArray(msg.chunks) && msg.chunks.length) {
            finalChunks = msg.chunks;
          }

          renderSummary(summary);
          renderSources(finalChunks);
          return;
        }

        if (eventName === "delta") {
          if (typeof msg.text === "string") {
            answerText.textContent += msg.text;
          }
          return;
        }

        if (eventName === "error") {
          answerText.textContent += `\n\nError: ${msg.error || "stream failed"}`;
          return;
        }

        if (eventName === "done") {
          if (typeof msg.answer === "string" && msg.answer) {
            answerText.textContent = msg.answer;
          }

          if (Array.isArray(msg.chunks)) {
            finalChunks = msg.chunks;
          }

          summary = {
            cache_hit: msg.cache_hit ?? summary.cache_hit,
            cache_score: msg.cache_score ?? summary.cache_score,
            retrieval_mode: msg.retrieval_mode ?? summary.retrieval_mode,
            hybrid_capable: msg.hybrid_capable ?? summary.hybrid_capable
          };

          renderSummary(summary);
          renderSources(finalChunks);
          return;
        }
      });

      renderSources(finalChunks);
    } catch (err) {
      if (String(err?.name || "") !== "AbortError") {
        result.innerHTML = `
          <div class="error-box">
            Request failed: ${escapeHtml(err?.message || String(err))}
          </div>
        `;
      }
    } finally {
      activeAbortController = null;
      setBusy(false);
    }
  }

  form.addEventListener("submit", submit);
})();