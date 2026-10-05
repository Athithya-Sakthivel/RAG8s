# Inference Pipeline — RAG Query and Response

The inference pipeline is the online execution path for RAG queries. It authenticates the request, checks exact and semantic caches, generates dense and sparse query embeddings, performs hybrid retrieval from Qdrant, optionally reranks the fused results, builds a grounded numbered prompt, generates the answer through AWS Bedrock with guardrails, validates citations, and writes the response to cache asynchronously.

The pipeline is optimized for low latency through parallel execution and non-blocking cache persistence. Retrieval and generation remain grounded in the indexed source chunks and their source metadata.

## Pipeline

```text
Request
  │
  ▼
OIDC Authentication
  │
  ▼
Exact Cache Lookup
  │
  ├── Hit ───────────────────────────────► Return Cached Response
  │
  ▼
Dense + Sparse Query Embeddings
        │
        ├─────────────── Parallel ────────────────┐
        │                                          │
        ▼                                          ▼
   Dense Query Vector                        Sparse Query Vector
        │                                          │
        └──────────────────┬───────────────────────┘
                           ▼
                  Semantic Cache Lookup
                           │
                    ├── Hit ─────────────► Return Cached Response
                    │
                    ▼
             Parallel Qdrant Retrieval
                    │
             ┌──────┴──────┐
             ▼             ▼
        Dense Search   Sparse Search
             │             │
             └──────┬──────┘
                    ▼
      Reciprocal Rank Fusion (RRF)
                    │
                    ▼
         Conditional Reranking
                    │
                    ▼
       Numbered Context Construction
                    │
                    ▼
     Grounded Prompt + Guardrails
                    │
                    ▼
            AWS Bedrock
                    │
                    ▼
        Citation Validation
                    │
                    ▼
          Final Response
                    │
                    └──────► Async Cache Write-Back
```

## 1. Request Authentication

The request first passes through OIDC authentication.

Only authenticated requests proceed to the RAG execution path. Authentication occurs before cache lookup, embedding generation, retrieval, and model invocation.

The authenticated request context must be preserved throughout the request lifecycle so cache entries and downstream operations are evaluated under the same access context.

## 2. Exact Cache Lookup

The first optimization is an exact cache lookup.

The cache key is derived from the normalized query and the relevant request context. An exact match represents a previously generated response for the same cacheable request.

### Exact-cache hit

```text
Request
  │
  ▼
Exact Cache Lookup
  │
  └── Hit ──► Cached Response
```

On an exact hit, the pipeline does not perform query embedding, semantic-cache lookup, Qdrant retrieval, reranking, or Bedrock generation.

### Exact-cache miss

Execution continues to query embedding generation.

## 3. Query Embedding Generation

Dense and sparse query embeddings are generated in parallel.

```text
                   Query
                     │
          ┌──────────┴──────────┐
          ▼                     ▼
   Dense Embedding       Sparse Embedding
          │                     │
          └──────────┬──────────┘
                     ▼
             Retrieval Stage
```

The dense vector is used for semantic similarity operations and dense retrieval.

The sparse representation is used for lexical/sparse retrieval.

The two embedding paths have independent failure handling so that a failure in one embedding service is not automatically treated as a failure of the other path.

## 4. Semantic Cache Lookup

After query embeddings are available, the inference service performs a semantic cache lookup.

The semantic cache compares the current query representation with previously cached queries and accepts a match only when the configured similarity policy is satisfied.

The documented cache policy uses approximately:

```text
Strict threshold:  ~0.84
Relaxed threshold: ~0.78
```

These thresholds should remain configuration values rather than hard-coded business logic.

### Semantic-cache hit

A valid semantic match can return the cached answer without executing Qdrant retrieval, reranking, or Bedrock generation.

### Semantic-cache miss

Execution continues to hybrid retrieval.

## 5. Hybrid Qdrant Retrieval

On a semantic-cache miss, dense and sparse retrieval are executed against Qdrant in parallel.

```text
                    Query
                      │
             ┌────────┴────────┐
             ▼                 ▼
       Dense Retrieval    Sparse Retrieval
             │                 │
             └────────┬────────┘
                      ▼
                     RRF
```

Dense retrieval ranks chunks according to semantic similarity.

Sparse retrieval provides lexical matching and preserves relevance for exact terms, names, identifiers, and other content where lexical overlap is important.

Each retrieval result retains the chunk payload and source metadata required by later prompt construction and citation validation.

## 6. Reciprocal Rank Fusion

Dense and sparse result lists are combined using Reciprocal Rank Fusion (RRF).

RRF operates on result rank rather than requiring dense and sparse scores to share the same scale.

The result is a single fused ranking used as the input to the optional reranking stage.

Duplicate chunks are consolidated so that the same indexed chunk is not unnecessarily represented multiple times in the final context.

## 7. Conditional Reranking

The fused result set may be passed through the configured reranking stage.

Reranking is conditional rather than mandatory for every request. The decision is based on the configured inference behavior and retrieval result set.

When reranking is enabled:

```text
Qdrant Results
     │
     ▼
RRF Fused Results
     │
     ▼
Reranker
     │
     ▼
Final Retrieval Order
```

When reranking is not required, the RRF ordering is used directly.

If the reranker is unavailable and the configured degradation path permits continuation, the pipeline proceeds with the fused ranking rather than failing an otherwise usable retrieval result.

## 8. Context Construction

The selected retrieval results are converted into a numbered context for generation.

Each context item receives a unique citation number within the request:

```text
[1] Retrieved chunk content
[2] Retrieved chunk content
[3] Retrieved chunk content
...
```

The numbering is generated for the current prompt and is the reference system used by the model when citing retrieved evidence.

The context entry retains the metadata associated with the originating chunk so that a generated citation can later be resolved to its source.

The prompt therefore has two distinct responsibilities:

```text
Retrieved content
    +
Citation numbering
    +
Grounding instructions
    +
Generation instructions
    +
Guardrails
```

## 9. Grounded Prompt and Bedrock Generation

The numbered retrieval context and the user query are assembled into the final grounded prompt.

The prompt is sent to AWS Bedrock with the configured model parameters and guardrails.

The model is expected to generate an answer grounded in the supplied retrieval context rather than relying on unsupported external information.

The inference path supports streaming generation so that generated output can be delivered without waiting for unrelated asynchronous work such as cache persistence.

## 10. Citation Validation

The generated response is validated against the retrieval context before citations are exposed as trusted source references.

Validation ensures that a cited reference corresponds to an actual numbered context item from the current request.

Conceptually:

```text
Generated Citation
       │
       ▼
Valid Context Number?
       │
   ┌───┴───┐
   │       │
  Yes      No
   │       │
   ▼       ▼
Resolve   Remove / Exclude
to Source Invalid Reference
```

Invalid or unresolvable references are removed or excluded rather than being returned as valid citations.

Validation prevents the response from exposing citations that do not correspond to retrieved evidence.

## 11. Source Resolution and Presigned Links

A validated citation is resolved through the retrieved chunk metadata back to the originating document.

The traceability chain is:

```text
Citation Number
      │
      ▼
Retrieved Chunk
      │
      ├── document_id
      ├── source_url / raw_key
      └── format-specific source offset
      │
      ▼
Original S3 Object
```

Where source download access is exposed, the service generates a presigned S3 URL.

The client therefore receives a direct source link without exposing the underlying S3 bucket through a public object URL.

## 12. Final Response

The final response contains the generated answer after citation validation.

The response path is independent of asynchronous cache persistence. A cache-write failure must not invalidate an otherwise successfully generated and validated response.

## 13. Asynchronous Cache Write-Back

After a successful response, cache persistence is performed asynchronously.

The write-back stage stores the response for future exact or semantic cache matches.

```text
Validated Response
       │
       ├──────────────► Client
       │
       └──────────────► Async Cache Write
```

The online response path does not wait for cache persistence to complete.

A failure to write the cache affects future cache-hit opportunities, but does not by itself require the already generated response to fail.

## 14. Failure and Graceful Degradation

The pipeline distinguishes between optional accelerators and required execution stages.

### Cache failures

An exact-cache or semantic-cache failure does not require the request to fail when the retrieval and generation path remains available.

```text
Cache Failure
     │
     ▼
Continue to Retrieval / Generation
```

### Embedding failures

Dense and sparse embedding generation are handled independently. Where the configured retrieval path permits it, one available representation can continue to downstream retrieval while the failed path is excluded.

### Reranker failure

If reranking is optional, a reranker failure falls back to the RRF ordering rather than failing the request.

### Cache write failure

Asynchronous cache persistence is non-critical to the current response. A write failure is logged/recorded without replacing a valid generated response with an error.

### Required-stage failures

When no valid fallback exists, failure of a required stage such as authentication, usable retrieval, or Bedrock generation results in request failure.

## 15. Latency-Critical Parallelism

The inference path explicitly parallelizes independent operations.

### Query embedding

```text
                  Query
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
       Dense                Sparse
     Embedding             Embedding
          │                   │
          └─────────┬─────────┘
                    ▼
             Next Stage
```

### Hybrid retrieval

```text
             Query Vectors
                  │
         ┌────────┴────────┐
         ▼                 ▼
      Qdrant             Qdrant
      Dense              Sparse
      Search             Search
         │                 │
         └────────┬────────┘
                  ▼
                 RRF
```

### Non-blocking persistence

```text
Generation
    │
    ▼
Citation Validation
    │
    ├────────────► Response
    │
    └────────────► Async Cache Write
```

These execution boundaries prevent independent network operations from unnecessarily adding their latencies sequentially.

## 16. End-to-End Execution Semantics

The normal cache-miss path is:

```text
1. Authenticate request
2. Normalize request/query
3. Check exact cache
4. Generate dense and sparse query embeddings in parallel
5. Check semantic cache
6. Retrieve dense and sparse results from Qdrant in parallel
7. Fuse results using RRF
8. Conditionally rerank fused results
9. Select final context
10. Number retrieval context for citations
11. Build grounded prompt with guardrails
12. Invoke AWS Bedrock
13. Validate generated citations
14. Resolve valid citations to source metadata
15. Generate presigned source links where applicable
16. Return final response
17. Write response to cache asynchronously
```

Cache-hit paths terminate earlier:

```text
Exact Cache Hit
    → Cached Response

Semantic Cache Hit
    → Cached Response
```

Neither cache-hit path requires a new LLM generation.

## 17. Traceability Requirements

Every citation exposed by the inference layer must remain traceable to the retrieval result from which it originated.

The minimum traceability relationship is:

```text
Response Citation
      ↓
Context Item Number
      ↓
Retrieved Chunk
      ↓
document_id / raw_key / source metadata
      ↓
Original S3 Document
```

The inference layer should not construct a source citation independently of the retrieved chunk metadata.

## 18. Observability

The inference service should expose stage-level logs and metrics sufficient to identify latency and failure at each major boundary.

Relevant signals include:

```text
request latency
exact-cache hit/miss
semantic-cache hit/miss
dense embedding latency/failure
sparse embedding latency/failure
dense retrieval latency/failure
sparse retrieval latency/failure
RRF result counts
reranker usage/latency/failure
Bedrock latency/failure
citation validation results
presigned URL generation failures
async cache write success/failure
```

A request correlation identifier should be retained across the inference stages so a single request can be traced from authentication through generation and cache persistence.

## 19. Key Properties

| Property | Implementation |
|---|---|
| Cache-first | Exact cache followed by semantic cache |
| Semantic retrieval | Dense query embedding and dense Qdrant search |
| Lexical retrieval | Sparse query embedding and sparse Qdrant search |
| Hybrid ranking | Reciprocal Rank Fusion |
| Optional precision stage | Conditional reranking |
| Grounding | Numbered retrieved context |
| Generation | AWS Bedrock |
| Safety control | Bedrock guardrails |
| Citation integrity | Post-generation citation validation |
| Source traceability | Citation → chunk → original S3 object |
| Source access | Presigned S3 URLs |
| Low latency | Parallel embeddings and parallel retrieval |
| Graceful degradation | Optional stages can fall back without failing the request |
| Non-blocking persistence | Asynchronous cache write-back |
| Observability | Stage-level logs and metrics |

---