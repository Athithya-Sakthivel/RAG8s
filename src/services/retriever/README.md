### The service is best understood as a layered decision tree:

```sh
request
  -> validate
  -> exact cache?
      yes -> return
      no  -> embed query
             -> semantic cache?
                 yes -> return and promote exact cache
                 no  -> retrieve docs
                        -> fuse
                        -> maybe rerank
                        -> build prompt
                        -> LLM or fallback
                        -> write cache
                        -> return
```
The service is a FastAPI retriever pipeline with three public behaviors:

* `/generate`: retrieve, answer with LLM, and optionally cache the answer
* `/retrieve`: retrieve only, no LLM answer generation. Used for offline eval.
* `/generate/stream`: same as `/generate`, but answer tokens are streamed as SSE

Internally it is a guarded pipeline with this order:

1. validate request
2. derive deterministic cache key
3. check exact cache
4. embed query
5. check semantic cache
6. retrieve documents from Qdrant
7. optionally rerank
8. build prompt
9. call Bedrock, or deterministic fallback if LLM is unavailable
10. write cache
11. return response or stream events

## 1) Process model and startup

At startup the app creates one shared `ServiceState` object containing:

* Qdrant store client
* dense embedder client
* sparse embedder client
* reranker client
* Bedrock client
* circuit breakers for each dependency
* health snapshot
* concurrency semaphore

Two background loops run for the lifetime of the app:

* health loop: every 10 seconds, probes Qdrant, dense, sparse, reranker, and Bedrock; updates `/readyz` state and the `service_ready` metric
* cache cleanup loop: periodically deletes expired semantic cache entries

The service is considered “ready” only when:

* docs collection exists
* Qdrant is reachable
* at least one retriever backend is healthy: dense or sparse

That means the system can be “live” without being “ready”.

## 2) Request-level defaults

The request models define the defaults that drive behavior.

### `/generate`

Defaults:

* `top_k = 5`
* `fetch_k = FETCH_K` from env, default 20
* `max_tokens = LLM_MAX_TOKENS`, default 512
* `allow_semantic_cache = True`
* `return_chunks = True`
* `model_name = BEDROCK_MODEL_ID`
* `corpus_version = CORPUS_VERSION`
* `prompt_version = PROMPT_VERSION`
* `retrieval_version = RETRIEVAL_VERSION`

### `/retrieve`

Defaults:

* `top_k = 5`
* `fetch_k = FETCH_K`
* `rerank = True`
* `include_cache = False`

### Streaming

`/generate/stream` uses the same request model and defaults as `/generate`.

## 3) Deterministic cache key invariant

The cache key is deterministic and must be stable across requests that should reuse the same answer.

It is built from:

* normalized query text
* corpus_version
* prompt_version
* retrieval_version
* model_name
* tenant_id if present
* top_k
* fetch_k

That means the same user query can intentionally map to different cache entries if any of those parameters change.

This is an important invariant:

* exact cache is only exact for the same logical retrieval/answer configuration
* a different model, corpus, or prompt version is a different cache key by design

## 4) Exact cache comes first

The first cache lookup is an exact lookup by cache_id.

Exact cache is checked before any retrieval work or LLM call.

If it hits:

* the request returns immediately
* no embeddings are computed
* no Qdrant retrieval is executed
* no reranking is executed
* no Bedrock call is executed

This is the fastest path and the strongest cache guarantee.

Exact cache is therefore a true short-circuit.

## 5) Query embedding happens after exact-cache miss

If exact cache misses, the service computes embeddings for the query.

It canonicalizes the query first, then:

* sends the query to the dense embedder if dense is healthy
* sends the query to the sparse embedder if sparse is healthy

This is done concurrently where possible.

The dense vector is normalized by the embedder client before use.

Sparse embedder returns token-index/value pairs that are converted into Qdrant sparse vector form.

Important invariants:

* if both embedders are healthy, the service tries both
* if one embedder fails, the other can still keep the pipeline alive
* the service never requires both embedders unless hybrid retrieval is being used

## 6) Semantic cache is next

If cache is ready and dense embedding succeeded, the service tries semantic cache lookup.

It uses the dense vector and applies filters for:

* cache group
* corpus_version
* prompt_version
* retrieval_version
* model_name
* non-expired entries only

There are two semantic thresholds:

* strict threshold
* relaxed threshold

Current logic is deliberately two-stage:

1. try strict threshold
2. if miss, try relaxed threshold

That gives the service a second chance for paraphrases and minor typos.

This is the reason you observed that lowering the threshold made semantic caching start to work.

### Semantic cache invariant

Semantic cache is only safe if the lookup and stored answer are still valid for the same versions and model. It is not “any similar query”; it is “similar query under the same corpus/prompt/retrieval/model conditions”.

### Semantic cache return behavior

If semantic cache hits:

* the cached answer is returned immediately
* no document retrieval is needed
* no Bedrock call is needed

In addition, the service schedules a background promotion task so the semantic hit is also stored under the exact cache key. This makes the next identical request faster and more reliable.

That promotion is intentionally not on the critical path.

## 7) Retrieval backend selection

If both dense and sparse vectors exist and the docs collection is marked hybrid-capable, the service performs hybrid retrieval.

Otherwise it falls back to whichever vector type is available:

* dense-only
* sparse-only
* hybrid

The retrieval logic is:

* hybrid: query both dense and sparse in parallel, then fuse with RRF
* dense-only: query dense
* sparse-only: query sparse

The service does not fail just because one modality fails, as long as one retrieval path remains available.

## 8) Fusion behavior

If both dense and sparse results are available, they are fused with Reciprocal Rank Fusion.

Important properties:

* fusion works on rank, not raw score magnitude
* documents that appear in both lists get stronger combined ranking
* duplicates are removed by chunk_id or point id
* fused results are sorted by fusion score, then retained for later stages

This is a core invariant:

* retrieval ranking is stabilized across retrieval modes using rank-based fusion rather than raw-score mixing

## 9) Optional reranking

After fusion, reranking may run depending on configuration and retrieval confidence.

Rerank is controlled by `RERANKER_MODE`:

* `DISABLE`: never rerank
* `ALWAYS`: always rerank
* `AUTO`: rerank only when needed

In AUTO mode, rerank is triggered when either:

* top fusion score is below `RERANK_AUTO_THRESHOLD`
* the top two fusion scores are too close, within `RERANK_MARGIN`

That means reranking is used as a confidence repair mechanism, not always as a mandatory stage.

The reranker only sees the top candidate slice up to `RERANKER_TOP_K` and `fetch_k`.

If reranking fails, the pipeline does not fail. It falls back to the fused order.

## 10) Prompt construction

The final candidate chunks are transformed into LLM input blocks.

The prompt builder does two things:

1. builds a compact human-readable prompt body
2. creates `ui_chunks` metadata for display and caching

It truncates content to prevent prompt overflow.

The prompt enforces citation discipline:

* only cited passage numbers are allowed
* filenames, URLs, page numbers, and other metadata are forbidden in the answer text

This matters because the system later filters citations against valid passage indexes.

## 11) LLM call behavior

If Bedrock is healthy, the service sends the prompt to Bedrock via `converse` or `converse_stream`.

If Bedrock is not healthy, or the call fails, the service falls back to deterministic summarization.

That fallback is not intended to be high quality; it is intended to keep the service functioning.

This gives the service a hard operational invariant:

* no request should fail purely because the LLM is unavailable if retrieval succeeded

For streaming, the service yields tokens as they arrive from Bedrock. It does not wait for the full answer before emitting output.

## 12) Post-processing of the answer

After the LLM response is assembled:

* citations are filtered to only valid passage indexes
* if answer is empty, the service uses deterministic summarization
* answer length is clipped to `MAX_PROMPT_CHARS`

This avoids malformed citations and excessive output.

## 13) Cache write behavior

After a successful LLM answer, the service writes the result to semantic cache.

This is important:

* the streamed answer is cached only after the stream completes and the full answer has been assembled
* the non-streaming answer is cached after the response has been produced
* exact cache and semantic cache share the same underlying collection, but are written with different hit types and payloads

If a dense vector is unavailable, the service cannot write semantic cache for that request.

## 14) `/generate` response contract

The `/generate` endpoint returns:

* `answer`
* `chunks` if enabled
* `retrieval`
* `cache`
* `cache_hit`
* `cache_score`
* `retrieval_mode`
* `hybrid_capable`

Its logic is:

* exact cache hit: return cached answer
* semantic cache hit: return cached answer and promote exact cache asynchronously
* retrieval miss: return 503 if no retriever backend is available
* retrieval success: answer with LLM and cache the result

## 15) `/retrieve` response contract

The `/retrieve` endpoint does not call the LLM.

It returns:

* `query`
* `chunks`
* `retrieval`
* `cache`
* `cache_hit`
* `cache_score`
* `retrieval_mode`
* `hybrid_capable`

The useful invariant here is that offline evaluation gets retrieval-only behavior without LLM cost or prompt dependence.

## 16) `/generate/stream` control flow

Streaming is almost the same as `/generate`, except the output is emitted in SSE events.

The sequence is:

1. build pipeline result with answer generation disabled at first
2. emit `start`
3. if exact or semantic cache hit, emit cached answer immediately and then `done`
4. else if no documents, emit a fallback answer
5. else stream tokens from Bedrock as `delta`
6. when complete, emit `done`
7. on error, emit `error`, then a fallback `delta`, then `done`

This design means streaming can still benefit from exact and semantic cache, and cached responses are immediate.

## 17) Circuit breaker and retry policy

Every external dependency has retry and circuit-breaker protection.

Dependencies wrapped by breakers include:

* cache
* retrieval
* dense embedding
* sparse embedding
* reranker
* LLM

Behavior:

* transient failures are retried up to `RETRY_MAX_ATTEMPTS`
* backoff is exponential with jitter
* non-retryable errors are not retried
* repeated failures open the breaker
* open breakers prevent repeated damage and reduce load on failing systems

This prevents the service from cascading into dependency storms.

## 18) Metrics and logging

The service emits Prometheus metrics for:

* request count and latency
* error count
* retries
* breaker openings
* cache lookups and writes
* embedding latency
* Qdrant query latency
* rerank latency
* LLM call latency
* retrieved docs count

Logging is configured through `setup_logging()` and `json_log()`.

Important behavior:

* `LOG_LEVEL` or `LOGLEVEL` controls the runtime logging level
* dependency libraries are silenced to WARNING
* structured JSON logs are emitted for important events

## 19) Core invariants

These are the main invariants the service tries to preserve:

* exact cache is deterministic and versioned
* semantic cache is approximate, versioned, and thresholded
* retrieval never depends on the LLM
* LLM never runs without retrieved chunks unless falling back
* cache writes occur only after a final answer exists
* streaming and non-streaming follow the same retrieval/cache logic
* failure of one dependency should not collapse the whole request if a safe fallback exists
* answers should only cite the retrieved passage numbers
* cache cleanup removes expired semantic entries but never changes live entries
* background health state must not block the request path

That is the actual control structure.

The main design choice is that cache and retrieval are not just optimizations. They are first-class control stages with deterministic versioning and failure containment.
