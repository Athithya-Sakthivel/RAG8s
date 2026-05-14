# Indexing Pipeline — Multi-Format RAG Ingestion

A Kubernetes CronJob that idempotently ingests raw documents from S3, normalizes them, splits content into chunks, generates dense and sparse embeddings, upserts points into Qdrant, and conditionally backs up qdrant collections to S3 when thresholds are met.

---

## Pipeline Steps (orchestrated sequentially by `indexing_pipeline.py`)

### 1. Pre-conversion (`pre_conversions.sh`)
- Scans `s3://<S3_BUCKET>/<S3_RAW_PREFIX>`, groups files by type into sub-prefixes (`audio/`, `pdfs/`, `csvs/`, `images/`…).
- Converts: `.docx/.doc` → PDF (LibreOffice); `.xls/.xlsx/.ods/.xlsm/.xlsb` → CSV; audio (`.mp3/.m4a/.wav/.flac/.ogg/.opus/.webm/.amr/.wma/.aiff/.aif`) → 16kHz mono WAV (FFmpeg).
- Moves converted files atomically into organized prefixes with metadata tags (etag, sample rate, `converted-from`). Idempotent: skips already-converted files unless `OVERWRITE_*` env flags are set.

### 2. Routing & Chunking (`router.py` + `parse_chunk/formats/*.py`)
- `router.py` lists every file under `S3_RAW_PREFIX`, detects format by extension/MIME, dispatches to the correct parser module (`pdf.py`, `md.py`, `wav.py`, `csv.py`, `jsonl.py`, `html.py`, `txt.py`, `images.py`, `pptx.py`).
- Each parser:
  - Downloads the file locally to temp storage.
  - Extracts text/structure: pymupdf+pdfplumber for PDF (with column reflow, table/image detection, optional OCR); faster-whisper for audio transcription; markdown-it-py for Markdown heading structure; OCR (rapidocr/tesseract) for images.
  - Splits into chunks by format-specific rules (pages, sentences, token windows, rows, slide text, heading sections, etc.).
  - Writes a **zstd-compressed Parquet** file to `s3://<S3_BUCKET>/<S3_CHUNKED_PREFIX>/<document_id>.parquet` with a canonical union schema.
  - Writes a `.manifest.json` next to the raw S3 object to mark it processed, containing file hash, size, row count, and timestamp.
- **Idempotency**: if a Parquet with the same `document_id` already exists (matched via manifest `file_hash` or `sha256(raw_key + LastModified)`), the parser skips the file unless `FORCE_PROCESS=true`.

### 3. Embedding & Indexing (`index.py`)
- Lists all Parquet files under `S3_CHUNKED_PREFIX` and loads rows into a canonical chunk schema.
- For each chunk, computes a **deterministic Qdrant point ID** from `chunk_id`.
- Queries Qdrant for existing IDs → deduplicates; only new chunks proceed.
- Calls the **dense embedder** (`DENSE_URL`) and/or **sparse embedder** (`SPARSE_URL`), with automatic recursive batch splitting when a request exceeds the embedder's size limit, plus exponential backoff with retries.
- Builds Qdrant `points` (dense + sparse vectors + full payload) and upserts in slices of `UPSERT_CHUNK` size with exponential backoff.

### 4. Conditional Backup (`run_qdrant_backup.py`)
See [Backup System](#backup-system) for full details.

---

## Supported Formats & Chunking Strategies

| Input Format | Normalization | Chunking Method | Schema Offsets |
|---|---|---|---|
| `.docx`, `.doc` | → PDF | Page-based, sentence groups, token windows | `page_number`, `line_start/end`, `figures` |
| `.xls`, `.xlsx`, `.ods`, `.xlsm`, `.xlsb` | → CSV | Row groups or token-window split of large rows | `row_range`, `token_range` |
| `.pdf` | Direct | Same as DOCX (pymupdf+pdfplumber, optional OCR) | `page_number`, `line_start/end`, `used_ocr`, `figures` |
| `.txt` | Direct | Sentence chunker (spaCy or regex), token windows with overlap | `line_start/end`, `token_count` |
| `.md`, `.markdown` | Direct | Heading-aware (markdown-it-py), merge small / split large sections | `line_start/end`, `headings`, `heading_path` |
| `.jsonl`, `.ndjson` | Direct | Row groups or token-window chunks; schema inferred from sampled rows | `row_range`, `token_range` |
| `.html` | Direct | Extracted text cleaned → sentence-chunked | `line_start/end` |
| `.pptx` | Direct | Slide text → sentence-chunked | `slide_number`, `line_start/end` |
| Images (`.jpg`, `.png`, `.tiff`, `.webp`, `.gif`, `.bmp`) | OCR (rapidocr/tesseract) | OCR text → sentence-chunked | `used_ocr=true` |
| Audio (`.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, etc.) | → 16kHz mono WAV | faster-whisper transcription → sentence → token-window chunks | `audio_start/end`, `parse_ms` |

### Universal Parquet Schema (key traceability fields)
- `document_id` — derived from `sha256(raw_key + LastModified)` or manifest `file_hash`.
- `chunk_id` — parser-specific stable ID (e.g., `{doc_id}_p{page}_{idx}`).
- `source_url` / `raw_key` — canonical S3 path.
- `token_count`, `token_range` — measured via `tiktoken` (if `TOKEN_ENCODER` set) else whitespace heuristic.
- `page_number`, `row_range`, `line_start/end`, `audio_start/end` — precise spatial/temporal offsets.
- `figures` — array of extracted table text or OCR'd image content.
- `semantic_region` — coarse positional label (`intro|early|middle|late|footer|unknown`) based on relative token position.
- `parser_version`, `timestamp`, `used_ocr` — lineage and debugging metadata.

Reverse mapping: any chunk can be traced back to exact original bytes via `source_url` + offset fields.

---

## Backup System

### Trigger Logic
Two independent thresholds, evaluated at the end of each indexing run (logical OR if both set):

| Variable | Condition |
|---|---|
| `BACKUP_THRESHOLD_DOCS` | Newly indexed documents since last backup ≥ N |
| `BACKUP_THRESHOLD_SECONDS` | Wall-clock seconds since last successful backup ≥ T |

If neither is set, backups are skipped. State is persisted in S3 at `s3://<S3_BACKUP_BUCKET>/qdrant/backups/backup_state.json` to survive pod restarts.

### Execution
1. Request snapshots for all Qdrant collections via REST API.
2. Download each snapshot locally, compute SHA256.
3. Upload to `s3://<S3_BACKUP_BUCKET>/qdrant/backups/<backup_id>/<collection_name>/<snapshot_name>`.
4. Write a `manifest.json` alongside the snapshot.
5. Clean up local snapshots unless `BACKUP_KEEP_LOCAL=true`.

### S3 Layout
```
s3://<S3_BACKUP_BUCKET>/
└── qdrant/
    └── backups/
        ├── backup_state.json
        └── <backup_id>/                     ← e.g., 20250115T143022Z-a1b2c3d4
            ├── manifest.json
            └── <collection_name>/
                └── <collection>-<timestamp>.snapshot
```

### Backup Manifest Schema
```json
{
  "backup_id": "20250115T143022Z-a1b2c3d4",
  "created_at": "2025-01-15T14:30:22Z",
  "env": "PRODUCTION",
  "trigger_reason": "threshold_docs",
  "collections": {
    "<collection_name>": {
      "snapshot_name": "<collection>-<timestamp>.snapshot",
      "s3_uri": "s3://bucket/qdrant/backups/<backup_id>/<collection>/<snapshot>",
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "size_bytes": 10485760,
      "qdrant_collection": "<collection_name>",
      "point_count": 1234567
    }
  },
  "thresholds_met": {
    "docs_since_last": 5000,
    "seconds_since_last": 86400
  },
  "retention_policy": {
    "max_backups": 30,
    "max_age_days": 90
  }
}
```

| Field | Description |
|---|---|
| `backup_id` | `{ISO8601_UTC}-{8_hex_random}`, used as S3 prefix |
| `created_at` | ISO 8601 UTC timestamp of manifest finalization |
| `env` | Deployment label from `ENV` or `NAMESPACE` |
| `trigger_reason` | `threshold_docs`, `threshold_time`, `combined`, or `manual` |
| `collections.<name>.sha256` | Computed locally after Qdrant download, before S3 upload |
| `thresholds_met` | Counter snapshot at trigger time |
| `retention_policy` | Optional cleanup rules |

### Retention Enforcement
When `retention_policy` is present, after each successful backup the script:
- Lists all backups under `s3://<S3_BACKUP_BUCKET>/qdrant/backups/`.
- Reads each manifest's `created_at`.
- Deletes any backup directory where count exceeds `max_backups` (oldest first) **or** age exceeds `max_age_days`.
- Atomically updates `backup_state.json`.

---

## Project Structure

```
src/infra/indexing_pipeline/
├── Dockerfile                        # Defines the runtime image for the indexer CronJob container.
├── indexing_pipeline.py              # Orchestrates the full indexing run: pre-conversion → parsing → indexing.
├── index.py                          # Loads chunks, embeds content, deduplicates, and upserts points into Qdrant.
├── parse_chunk/
│   ├── __init__.py
│   ├── router.py                     # Routes raw files to the correct parser based on type and extension.
│   └── formats/
│       ├── __init__.py
│       ├── _csv.py                   # Parses CSV files into row- or token-based text chunks.
│       ├── _html.py                  # Extracts and cleans HTML or remote web content into text chunks.
│       ├── images.py                 # Performs OCR and image-text extraction used by other parsers.
│       ├── jsonl.py                  # Parses JSONL records into normalized text chunks.
│       ├── md.py                     # Parses Markdown files into structured text chunks.
│       ├── pdf.py                    # Extracts text, figures, and OCR from PDFs and chunks by page/window.
│       ├── pptx.py                   # Extracts slide text and structure from PPTX files.
│       ├── txt.py                    # Normalizes and chunks plain text files.
│       └── wav.py                    # Transcribes and chunks audio files into text segments.
├── pre_conversions.sh                # Normalizes raw inputs by converting docs, sheets, and audio to canonical formats.
├── requirements.txt                  # Pins Python dependencies required by the indexing pipeline.
└── run_qdrant_backup.py              # Creates Qdrant snapshots and uploads to S3 when thresholds are met.
```

---

## Key Design Properties
- **Idempotent & Deterministic**: manifest-based skip logic; point IDs derived from `chunk_id` ensure deduplication.
- **Atomic S3 writes**: temp-then-rename pattern prevents partial reads.
- **Resilient**: recursive batch splitting on embedder failures; exponential backoff with jitter on S3/Qdrant/embedder calls.
- **Traceable**: full reverse mapping from any chunk back to original S3 bytes via `source_url` + offset fields; parser version and timestamp in every Parquet.
- **Observable**: structured logging to stdout/stderr captured by CronJob for centralized monitoring.
---