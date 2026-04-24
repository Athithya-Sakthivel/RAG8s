#!/usr/bin/env python3
import os
import sys

import boto3

# -----------------------------
# Required env
# -----------------------------
S3_BUCKET = os.environ.get("S3_BUCKET") or os.environ.get("DATA_S3_BUCKET")
if not S3_BUCKET:
    sys.stderr.write("ERROR: S3_BUCKET (or DATA_S3_BUCKET) required\n")
    sys.exit(2)

S3_RAW_PREFIX = os.environ.get("S3_RAW_PREFIX", "data/raw/")
S3_RAW_PREFIX = S3_RAW_PREFIX.lstrip("/").rstrip("/") + "/"

AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")

# -----------------------------
# AWS client (idempotent)
# -----------------------------
session = boto3.session.Session(region_name=AWS_REGION) if AWS_REGION else boto3.session.Session()
s3 = session.client("s3")


# -----------------------------
# helpers
# -----------------------------
def list_keys():
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_RAW_PREFIX):
        for obj in page.get("Contents", []):
            yield obj["Key"]


def normalize_ext(name: str) -> str:
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def move_object(src_key: str, dst_key: str) -> None:
    copy_source = {"Bucket": S3_BUCKET, "Key": src_key}

    try:
        s3.copy_object(CopySource=copy_source, Bucket=S3_BUCKET, Key=dst_key)
        s3.delete_object(Bucket=S3_BUCKET, Key=src_key)
        sys.stderr.write(
            f"MOVE s3://{S3_BUCKET}/{src_key} -> s3://{S3_BUCKET}/{dst_key}\n"
        )
    except Exception as e:
        sys.stderr.write(f"ERROR move failed {src_key}: {e}\n")


def classify(ext: str) -> str:
    if ext in {"mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "webm", "amr", "wma", "aiff", "aif"}:
        return "audio/"
    if ext in {"jpg", "jpeg", "png", "webp", "tif", "tiff", "bmp", "gif"}:
        return "images/"
    if ext == "pdf":
        return "pdfs/"
    if ext in {"doc", "docx"}:
        return "docs/"
    if ext in {"ppt", "pptx"}:
        return "ppts/"
    if ext == "txt":
        return "txts/"
    if ext == "csv":
        return "csvs/"
    if ext == "md":
        return "mds/"
    if ext == "html":
        return "htmls/"
    if ext == "jsonl":
        return "jsonls/"
    return "others/"


# -----------------------------
# main logic (idempotent)
# -----------------------------
def reorganize():
    for key in list_keys():
        if not key or key.endswith("/") or key.endswith(".manifest.json"):
            continue

        filename = os.path.basename(key)
        ext = normalize_ext(filename)
        subdir = classify(ext)

        dst_key = f"{S3_RAW_PREFIX}{subdir}{filename}"

        if dst_key == key:
            continue

        move_object(key, dst_key)


def main():
    try:
        reorganize()
    except KeyboardInterrupt:
        sys.stderr.write("Interrupted\n")
        sys.exit(130)
    except Exception as e:
        sys.stderr.write(f"FATAL: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
