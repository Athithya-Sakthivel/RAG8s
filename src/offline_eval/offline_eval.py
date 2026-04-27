#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import mlflow

LOG = logging.getLogger("offline_eval")
DATASET_DEFAULT = Path(__file__).with_name("golden_dataset.json")
OUT_DIR_DEFAULT = Path(__file__).resolve().parent / "offline_eval_artifacts"

ANSWER_PROMPT_TEMPLATE = os.getenv(
    "LLM_PROMPT_TEMPLATE",
    (
        "You are a knowledge assistant who must explain explicitly to an end-user by referring ONLY to the provided passages BELOW"
        "You MUST end every passage with a citation in the exact format [n], where n is one of the numbered passage blocks."
        "Use ONLY the provided passage numbers. Do NOT output filenames, secrets, URLs, page numbers, or any other metadata. Do NOT invent citations."
        "PASSAGES:\n{passages}\n\n"
        "QUESTION: {question}\n\n"
        "Answer:"
    ),
)

os.environ.setdefault("MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION", "true")
os.environ.setdefault("MLFLOW_GENAI_EVAL_MAX_WORKERS", "1")
os.environ.setdefault("MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS", "1")
os.environ.setdefault("MLFLOW_GENAI_EVAL_PREDICT_RATE_LIMIT", "0")


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("mlflow").setLevel(logging.INFO)


def norm_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def words(value: Any) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", norm_text(value)))


def similarity(a: Any, b: Any) -> float:
    return SequenceMatcher(None, norm_text(a), norm_text(b)).ratio()


def sentence_split(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def truncate_text(text: str, max_chars: int) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    boundary = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
    if boundary >= int(max_chars * 0.6):
        cut = cut[: boundary + 1]
    return cut.rstrip() + "…"


def load_dataset(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("golden dataset must be a JSON list")
    return data


def get_json(url: str, timeout_s: float = 10.0) -> tuple[int, dict[str, Any]]:
    req = Request(url, method="GET", headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8")
        return resp.status, json.loads(raw) if raw.strip() else {}


def post_json(
    url: str,
    payload: dict[str, Any],
    timeout_s: float = 120.0,
    *,
    max_attempts: int = 2,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        req = Request(url, data=body, method="POST", headers=headers)
        try:
            with urlopen(req, timeout=timeout_s) as resp:
                raw = resp.read().decode("utf-8")
                if not raw.strip():
                    return resp.status, {}
                try:
                    return resp.status, json.loads(raw)
                except Exception:
                    return resp.status, {"raw": raw}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
            try:
                parsed = json.loads(raw) if raw else {}
            except Exception:
                parsed = {"raw": raw}
            parsed["http_status"] = exc.code
            if attempt < max_attempts and exc.code in {429, 500, 502, 503, 504}:
                wait = min(2.0 * attempt, 5.0)
                LOG.warning(
                    "retryable HTTP %s from %s (attempt %s/%s); sleeping %.1fs",
                    exc.code,
                    url,
                    attempt,
                    max_attempts,
                    wait,
                )
                time.sleep(wait)
                last_error = exc
                continue
            return exc.code, parsed
        except URLError as exc:
            last_error = exc
            if attempt < max_attempts:
                wait = min(2.0 * attempt, 5.0)
                LOG.warning(
                    "network error calling %s (attempt %s/%s): %s; sleeping %.1fs",
                    url,
                    attempt,
                    max_attempts,
                    exc,
                    wait,
                )
                time.sleep(wait)
                continue
            return 599, {"error": str(exc)}
        except Exception as exc:
            last_error = exc
            if attempt < max_attempts:
                wait = min(2.0 * attempt, 5.0)
                LOG.warning(
                    "unexpected error calling %s (attempt %s/%s): %s; sleeping %.1fs",
                    url,
                    attempt,
                    max_attempts,
                    exc,
                    wait,
                )
                time.sleep(wait)
                continue
            return 599, {"error": str(exc)}

    return 599, {"error": str(last_error) if last_error else "request failed"}


def wait_for_ready(base_url: str, timeout_s: float = 120.0, interval_s: float = 2.0) -> dict[str, Any]:
    ready_url = f"{base_url.rstrip('/')}/readyz"
    start = time.monotonic()

    LOG.info("waiting for retriever readiness at %s", ready_url)
    while True:
        try:
            status, payload = get_json(ready_url, timeout_s=5.0)
            if status == 200 and payload.get("status") == "ready":
                LOG.info("retriever is ready: %s", payload)
                return payload
            LOG.info(
                "not ready yet: status=%s qdrant=%s docs=%s cache=%s dense=%s sparse=%s reranker=%s bedrock=%s bootstrap_error=%s",
                payload.get("status"),
                payload.get("qdrant"),
                payload.get("docs_collection_ready"),
                payload.get("cache_collection_ready"),
                payload.get("dense"),
                payload.get("sparse"),
                payload.get("reranker"),
                payload.get("bedrock"),
                payload.get("bootstrap_error"),
            )
        except Exception as exc:
            LOG.info("ready check failed: %s", exc)

        if time.monotonic() - start >= timeout_s:
            raise TimeoutError(f"timed out waiting for retriever readiness at {ready_url}")

        time.sleep(interval_s)


def build_prompt(chunks: list[dict[str, Any]], query: str, max_content_chars: int = 2500) -> tuple[str, list[str]]:
    blocks: list[str] = []
    llm_lines: list[str] = []

    for idx, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            continue

        heading_value = chunk.get("title") or chunk.get("heading_path") or chunk.get("headings") or ""
        if isinstance(heading_value, (list, tuple)):
            heading = " - ".join(norm_text(x) for x in heading_value if norm_text(x))
        else:
            heading = str(heading_value).strip()

        content = chunk.get("content") or chunk.get("text") or chunk.get("html") or ""
        content = truncate_text(str(content), max_content_chars)

        block_lines = [f"[{idx}]"]
        if heading:
            block_lines.append(f"Heading: {heading}")
        if content:
            block_lines.append(f"Content: {content}")
        blocks.append("\n".join(block_lines))

        llm_lines.append(
            json.dumps(
                {
                    "index": idx,
                    "heading": heading or None,
                    "content": content,
                    "chunk_id": chunk.get("chunk_id") or chunk.get("id") or "",
                },
                ensure_ascii=False,
            )
        )

    prompt_body = "\n\n".join(blocks) + f"\n\nQUESTION: {query}\n\nAnswer:"
    prompt = ANSWER_PROMPT_TEMPLATE.format(passages=prompt_body, question=query)
    return prompt, llm_lines


def extract_bedrock_text(resp: dict[str, Any]) -> str:
    output = resp.get("output") or {}
    message = output.get("message") or {}
    content = message.get("content") or []
    pieces: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                txt = block.get("text")
                if txt:
                    pieces.append(str(txt))
            elif isinstance(block, str):
                pieces.append(block)
    if pieces:
        return "".join(pieces).strip()

    for key in ("outputText", "completion"):
        cur: Any = resp
        if isinstance(cur, dict) and key in cur and isinstance(cur[key], str):
            return cur[key].strip()
    return ""


def is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, (TypeError, ValueError)):
        return False
    msg = str(exc).lower()
    return any(token in msg for token in ("timeout", "throttl", "temporarily", "connection", "unavailable", "429", "502", "503", "504"))


def bedrock_generate(
    *,
    prompt: str,
    region: str,
    model_id: str,
    max_tokens: int,
    temperature: float,
    timeout_s: float,
    guardrail_identifier: str = "",
    guardrail_version: str = "",
    max_attempts: int = 3,
) -> str:
    import boto3
    from botocore.config import Config as BotoConfig

    session = boto3.session.Session(region_name=region)
    client = session.client(
        "bedrock-runtime",
        config=BotoConfig(
            connect_timeout=timeout_s,
            read_timeout=timeout_s,
            retries={"max_attempts": max_attempts, "mode": "standard"},
        ),
    )

    payload: dict[str, Any] = {
        "modelId": model_id,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": int(max_tokens), "temperature": float(temperature)},
    }
    if guardrail_identifier.strip():
        guardrail_cfg: dict[str, Any] = {"guardrailIdentifier": guardrail_identifier.strip(), "trace": "enabled"}
        if guardrail_version.strip():
            guardrail_cfg["guardrailVersion"] = guardrail_version.strip()
        payload["guardrailConfig"] = guardrail_cfg

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.converse(**payload)
            text = extract_bedrock_text(resp if isinstance(resp, dict) else {})
            if not text:
                raise RuntimeError("bedrock returned empty content")
            return text
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts and is_retryable_exception(exc):
                wait = min(2.0 * attempt, 5.0)
                LOG.warning("bedrock attempt %s/%s failed: %s; sleeping %.1fs", attempt, max_attempts, exc, wait)
                time.sleep(wait)
                continue
            break

    raise RuntimeError(str(last_exc) if last_exc else "bedrock failed")


def deterministic_summarize(text: str, max_chars: int = 800) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    sentences = sentence_split(text)
    if not sentences:
        return truncate_text(text, max_chars)
    out: list[str] = []
    total = 0
    for sent in sentences:
        out.append(sent)
        total += len(sent)
        if len(out) >= 2 or total >= max_chars:
            break
    return truncate_text(" ".join(out), max_chars)


def _item_aliases(item: dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    if not isinstance(item, dict):
        return aliases

    for key in (
        "chunk_id",
        "doc_id",
        "doc_uri",
        "source_url",
        "source_doc_id",
        "document_id",
    ):
        value = item.get(key)
        if value not in (None, ""):
            aliases.add(f"{key}:{norm_text(value)}")

    if item.get("chunk_index") is not None:
        aliases.add(f"chunk_index:{item.get('chunk_index')}")
    if item.get("page_number") is not None:
        aliases.add(f"page_number:{item.get('page_number')}")
    if item.get("line_start") is not None or item.get("line_end") is not None:
        aliases.add(f"line_range:{item.get('line_start','')}-{item.get('line_end','')}")
    return aliases


def calc_retrieval_metrics(
    chunks: list[dict[str, Any]],
    expected_retrieved_context: list[dict[str, Any]] | None,
) -> dict[str, float]:
    expected_items = [x for x in (expected_retrieved_context or []) if isinstance(x, dict)]
    retrieved_items = [x for x in (chunks or []) if isinstance(x, dict)]

    if not expected_items:
        return {
            "recall_at_k": 0.0,
            "hit_rate_at_k": 0.0,
            "mrr": 0.0,
        }

    expected_aliases = [_item_aliases(x) for x in expected_items]
    retrieved_aliases = [_item_aliases(x) for x in retrieved_items]

    used_retrieved: set[int] = set()
    matched_expected = 0
    first_hit_rank = 0

    for e_aliases in expected_aliases:
        found_idx = None
        for r_idx, r_aliases in enumerate(retrieved_aliases):
            if r_idx in used_retrieved:
                continue
            if e_aliases & r_aliases:
                found_idx = r_idx
                break
        if found_idx is not None:
            used_retrieved.add(found_idx)
            matched_expected += 1
            if first_hit_rank == 0:
                first_hit_rank = found_idx + 1

    recall = matched_expected / max(1, len(expected_aliases))
    hit_rate = 1.0 if matched_expected > 0 else 0.0
    mrr = 1.0 / float(first_hit_rank) if first_hit_rank else 0.0

    return {
        "recall_at_k": float(recall),
        "hit_rate_at_k": float(hit_rate),
        "mrr": float(mrr),
    }


def calc_citation_integrity(answer: str, chunks: list[dict[str, Any]]) -> float:
    citations = [int(x) for x in re.findall(r"\[(\d+)\]", answer or "")]
    if not citations:
        return 0.0
    valid = set(range(1, len(chunks) + 1))
    valid_count = sum(1 for c in citations if c in valid)
    return valid_count / len(citations)


def calc_fact_coverage(answer: str, chunks: list[dict[str, Any]], expected_facts: list[str] | None) -> float:
    if not expected_facts:
        return 0.0

    answer_n = norm_text(answer)
    context = " ".join(str(ch.get("content") or "") for ch in chunks if isinstance(ch, dict))
    context_n = norm_text(context)
    answer_words = words(answer)
    context_words = words(context)

    hits = 0
    for fact in expected_facts:
        fact_n = norm_text(fact)
        if not fact_n:
            continue
        if fact_n in answer_n or fact_n in context_n:
            hits += 1
            continue
        fact_words = words(fact)
        if fact_words and len(fact_words & (answer_words | context_words)) / len(fact_words) >= 0.7:
            hits += 1
    return hits / max(1, len(expected_facts))


def calc_groundedness(answer: str, chunks: list[dict[str, Any]]) -> float:
    sentences = sentence_split(answer)
    if not sentences:
        return 0.0

    context = " ".join(str(ch.get("content") or "") for ch in chunks if isinstance(ch, dict))
    context_n = norm_text(context)
    context_words = words(context)

    supported = 0
    for sent in sentences:
        sent_n = norm_text(sent)
        if not sent_n:
            continue
        if sent_n in context_n:
            supported += 1
            continue
        sent_words = words(sent)
        if sent_words and len(sent_words & context_words) / len(sent_words) >= 0.7:
            supported += 1
    return supported / max(1, len(sentences))


def calc_response_similarity(answer: str, expected_response: str | None) -> float:
    if not expected_response:
        return 0.0
    return similarity(answer, expected_response)


def sanitize_citations(answer: str, valid_indexes: list[int]) -> str:
    if not answer:
        return answer

    answer = re.sub(
        r"\[.*?(source_url|page_number|file_name|row_range|token_range|audio_range|headings|heading_path|chunk_id).*?\]",
        " ",
        answer,
        flags=re.IGNORECASE,
    )

    def repl(match: re.Match[str]) -> str:
        num = int(match.group(1))
        return f"[{num}]" if num in valid_indexes else ""

    answer = re.sub(r"\[(\d+)\]", repl, answer)
    answer = re.sub(r"https?://\S+", "", answer)
    answer = re.sub(r"\s+", " ", answer).strip()
    return answer


@dataclass
class SummaryAcc:
    records: int = 0
    success_count: int = 0
    recall_sum: float = 0.0
    hit_rate_sum: float = 0.0
    mrr_sum: float = 0.0
    fact_coverage_sum: float = 0.0
    groundedness_sum: float = 0.0
    response_similarity_sum: float = 0.0
    citation_integrity_sum: float = 0.0
    error_count: int = 0

    def add(self, row: dict[str, Any]) -> None:
        self.records += 1
        self.recall_sum += float(row.get("recall_at_k") or 0.0)
        self.hit_rate_sum += float(row.get("hit_rate_at_k") or 0.0)
        self.mrr_sum += float(row.get("mrr") or 0.0)
        self.fact_coverage_sum += float(row.get("fact_coverage") or 0.0)
        self.groundedness_sum += float(row.get("groundedness") or 0.0)
        self.response_similarity_sum += float(row.get("response_similarity") or 0.0)
        self.citation_integrity_sum += float(row.get("citation_integrity") or 0.0)
        if row.get("error"):
            self.error_count += 1
        else:
            self.success_count += 1

    def summary(self) -> dict[str, Any]:
        n = max(1, self.records)
        return {
            "meta": {
                "records": self.records,
            },
            "performance": {
                "success_rate": round(self.success_count / n, 4),
            },
            "retrieval": {
                "recall_at_k": round(self.recall_sum / n, 4),
                "hit_rate_at_k": round(self.hit_rate_sum / n, 4),
                "mrr": round(self.mrr_sum / n, 4),
            },
            "generation": {
                "fact_coverage": round(self.fact_coverage_sum / n, 4),
                "groundedness": round(self.groundedness_sum / n, 4),
                "response_similarity": round(self.response_similarity_sum / n, 4),
            },
            "citations": {
                "citation_integrity": round(self.citation_integrity_sum / n, 4),
            },
            "errors": {
                "rate": round(self.error_count / n, 4),
            },
        }


def flatten_summary(summary: dict[str, Any]) -> dict[str, float]:
    return {
        "performance_success_rate": float(summary["performance"]["success_rate"]),
        "retrieval_recall_at_k": float(summary["retrieval"]["recall_at_k"]),
        "retrieval_hit_rate_at_k": float(summary["retrieval"]["hit_rate_at_k"]),
        "retrieval_mrr": float(summary["retrieval"]["mrr"]),
        "generation_fact_coverage": float(summary["generation"]["fact_coverage"]),
        "generation_groundedness": float(summary["generation"]["groundedness"]),
        "generation_response_similarity": float(summary["generation"]["response_similarity"]),
        "citations_citation_integrity": float(summary["citations"]["citation_integrity"]),
        "errors_rate": float(summary["errors"]["rate"]),
    }


def main() -> int:
    configure_logging()

    parser = argparse.ArgumentParser(description="Offline evaluation for the retriever service.")
    parser.add_argument("--dataset", type=Path, default=DATASET_DEFAULT)
    parser.add_argument("--base-url", default=os.getenv("RETRIEVER_BASE_URL", "http://127.0.0.1:8203"))
    parser.add_argument("--experiment", default=os.getenv("MLFLOW_EXPERIMENT_NAME", "retriever-offline-eval"))
    parser.add_argument("--tracking-uri", default=os.getenv("MLFLOW_TRACKING_URI"))
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR_DEFAULT)
    parser.add_argument("--max-records", type=int, default=0)
    parser.add_argument("--top-k", type=int, default=int(os.getenv("EVAL_TOP_K", "5")))
    parser.add_argument("--fetch-k", type=int, default=int(os.getenv("EVAL_FETCH_K", "10")))
    parser.add_argument("--max-tokens", type=int, default=int(os.getenv("EVAL_MAX_TOKENS", "256")))
    parser.add_argument("--timeout-s", type=float, default=float(os.getenv("EVAL_TIMEOUT_S", "120")))
    parser.add_argument("--log-every", type=int, default=5)
    parser.add_argument("--run-name", default=f"offline-eval-{time.strftime('%Y%m%d-%H%M%S')}")
    parser.add_argument("--bedrock-region", default=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-south-1")
    parser.add_argument("--bedrock-model-id", default=os.getenv("BEDROCK_MODEL_ID") or os.getenv("AWS_BEDROCK_MODEL_ID") or "meta.llama3-8b-instruct-v1:0")
    parser.add_argument("--bedrock-temperature", type=float, default=float(os.getenv("LLM_TEMPERATURE", "0.1")))
    parser.add_argument("--guardrail-identifier", default=os.getenv("BEDROCK_GUARDRAIL_IDENTIFIER", ""))
    parser.add_argument("--guardrail-version", default=os.getenv("BEDROCK_GUARDRAIL_VERSION", ""))
    args = parser.parse_args()

    if args.tracking_uri:
        mlflow.set_tracking_uri(args.tracking_uri)
    mlflow.set_experiment(args.experiment)

    LOG.info("loading dataset: %s", args.dataset)
    raw = load_dataset(args.dataset)
    if args.max_records and args.max_records > 0:
        raw = raw[: args.max_records]

    LOG.info("dataset size=%d", len(raw))
    ready = wait_for_ready(args.base_url, timeout_s=args.timeout_s)
    LOG.info(
        "ready state qdrant=%s docs=%s cache=%s dense=%s sparse=%s reranker=%s bedrock=%s hybrid=%s",
        ready.get("qdrant"),
        ready.get("docs_collection_ready"),
        ready.get("cache_collection_ready"),
        ready.get("dense"),
        ready.get("sparse"),
        ready.get("reranker"),
        ready.get("bedrock"),
        ready.get("hybrid_capable"),
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = args.out_dir / "offline_eval_rows.jsonl"
    summary_path = args.out_dir / "summary.json"

    acc = SummaryAcc()
    processed = 0

    with mlflow.start_run(run_name=args.run_name):
        mlflow.log_params(
            {
                "base_url": args.base_url,
                "dataset_path": str(args.dataset),
                "records_requested": len(raw),
                "top_k": args.top_k,
                "fetch_k": args.fetch_k,
                "max_tokens": args.max_tokens,
                "bedrock_region": args.bedrock_region,
                "bedrock_model_id": args.bedrock_model_id,
            }
        )

        LOG.info("starting direct offline benchmark loop")
        with rows_path.open("w", encoding="utf-8") as rows_file:
            for idx, rec in enumerate(raw, start=1):
                if not isinstance(rec, dict):
                    LOG.warning("skipping non-object record at index=%d", idx)
                    continue

                record_id = str(rec.get("dataset_record_id") or f"row-{idx:04d}")
                inputs = dict(rec.get("inputs") or {})
                expectations = dict(rec.get("expectations") or {})
                query = str(inputs.get("query") or "").strip()

                if not query:
                    LOG.warning("[%d/%d] skipping record_id=%s because query is empty", idx, len(raw), record_id)
                    continue

                started = time.perf_counter()
                LOG.info("[%d/%d] start record_id=%s query=%s", idx, len(raw), record_id, query[:160])

                payload = {
                    "query": query,
                    "top_k": args.top_k,
                    "fetch_k": args.fetch_k,
                    "return_chunks": True,
                    "allow_semantic_cache": False,
                    "debug": False,
                    "enable_tracing": False,
                    "max_tokens": args.max_tokens,
                    "tenant_id": inputs.get("tenant_id"),
                    "corpus_version": inputs.get("corpus_version"),
                    "prompt_version": inputs.get("prompt_version"),
                    "retrieval_version": inputs.get("retrieval_version"),
                }

                status, resp = post_json(
                    f"{args.base_url.rstrip('/')}/generate",
                    payload,
                    timeout_s=args.timeout_s,
                    max_attempts=2,
                )

                chunks: list[dict[str, Any]] = []
                retrieval_meta: dict[str, Any] = {}
                error: str | None = None
                answer = ""

                if status == 200 and isinstance(resp, dict):
                    chunks = list(resp.get("chunks") or [])
                    retrieval_meta = dict(resp.get("retrieval") or {})
                    answer = str(resp.get("answer") or "")
                    valid_indexes = list(range(1, len(chunks) + 1))
                    answer = sanitize_citations(answer, valid_indexes)
                    if not answer.strip():
                        answer = deterministic_summarize(" ".join(str(ch.get("content") or "") for ch in chunks))
                else:
                    error = str(resp.get("error") or resp.get("detail") or f"http_{status}") if isinstance(resp, dict) else f"http_{status}"
                    answer = deterministic_summarize(" ".join(str(ch.get("content") or "") for ch in chunks))
                    if not answer.strip():
                        answer = "llm unavailable"

                latency_ms = (time.perf_counter() - started) * 1000.0
                expected_context = expectations.get("expected_retrieved_context") or []
                expected_facts = expectations.get("expected_facts") or []
                expected_response = expectations.get("expected_response")

                retrieval_metrics = calc_retrieval_metrics(chunks, expected_context)
                row = {
                    "dataset_record_id": record_id,
                    "eval_index": idx,
                    "query": query,
                    "answer": answer,
                    "chunks": chunks,
                    "retrieval": retrieval_meta,
                    "latency_ms": round(latency_ms, 3),
                    "error": error,
                    "recall_at_k": retrieval_metrics["recall_at_k"],
                    "hit_rate_at_k": retrieval_metrics["hit_rate_at_k"],
                    "mrr": retrieval_metrics["mrr"],
                    "fact_coverage": calc_fact_coverage(answer, chunks, expected_facts),
                    "groundedness": calc_groundedness(answer, chunks),
                    "response_similarity": calc_response_similarity(answer, expected_response),
                    "citation_integrity": calc_citation_integrity(answer, chunks),
                }

                rows_file.write(json.dumps(row, ensure_ascii=False) + "\n")
                rows_file.flush()

                acc.add(row)
                processed += 1

                LOG.info(
                    "[%d/%d] done record_id=%s status=%s latency_ms=%.1f chunks=%d recall=%.3f hit=%.3f fact=%.3f sim=%.3f error=%s",
                    idx,
                    len(raw),
                    record_id,
                    status,
                    latency_ms,
                    len(chunks),
                    row["recall_at_k"],
                    row["hit_rate_at_k"],
                    row["fact_coverage"],
                    row["response_similarity"],
                    error or "-",
                )

                if args.log_every > 0 and processed % args.log_every == 0:
                    LOG.info(
                        "progress %d/%d avg_recall=%.3f avg_hit=%.3f avg_fact_coverage=%.3f",
                        processed,
                        len(raw),
                        acc.recall_sum / max(1, acc.records),
                        acc.hit_rate_sum / max(1, acc.records),
                        acc.fact_coverage_sum / max(1, acc.records),
                    )

        summary = acc.summary()
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        mlflow.log_metrics(flatten_summary(summary))
        mlflow.log_artifact(str(rows_path), artifact_path="offline_eval")
        mlflow.log_artifact(str(summary_path), artifact_path="offline_eval")
        LOG.info("evaluation complete")
        print(json.dumps(summary, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
