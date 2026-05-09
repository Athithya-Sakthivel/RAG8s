#!/usr/bin/env bash
S3_BUCKET="${S3_BUCKET:?S3_BUCKET required}"
S3_RAW_PREFIX="${S3_RAW_PREFIX:-data/raw/}"
S3_RAW_PREFIX="${S3_RAW_PREFIX#/}"
S3_RAW_PREFIX="${S3_RAW_PREFIX%/}/"
AWS_REGION="${AWS_REGION:-}"
[ -n "${AWS_REGION:-}" ] && export AWS_DEFAULT_REGION="$AWS_REGION"

OVERWRITE_ALL_AUDIO_FILES="${OVERWRITE_ALL_AUDIO_FILES:-true}"
OVERWRITE_OTHER_TO_PDF="${OVERWRITE_OTHER_TO_PDF:-true}"
OVERWRITE_SPREADSHEETS_WITH_CSV="${OVERWRITE_SPREADSHEETS_WITH_CSV:-true}"
TMP_DIR="${TMP_DIR:-/tmp/preconv}"
FFMPEG_BIN="${FFMPEG_BIN:-ffmpeg}"
SOFFICE_BIN="${SOFFICE_BIN:-soffice}"
AWSCLI="${AWSCLI:-aws}"
JQ_BIN="${JQ_BIN:-jq}"

mkdir -p "$TMP_DIR/src" "$TMP_DIR/out"

# require tools (can be relaxed by setting SKIP_PRECONVERT_STRICT=1)
for cmd in "$AWSCLI" "$JQ_BIN" "$FFMPEG_BIN" "$SOFFICE_BIN"; do
  if ! command -v "$cmd" >/dev/null 2>&1 ; then
    echo "WARN: $cmd not found" >&2
    if [ -z "${SKIP_PRECONVERT_STRICT:-}" ]; then
      echo "set SKIP_PRECONVERT_STRICT=1 to continue without this tool" >&2
      exit 2
    fi
  fi
done

if command -v java >/dev/null 2>&1; then
  JAVA_BIN="$(readlink -f "$(which java)" 2>/dev/null || true)"
  [ -n "${JAVA_BIN:-}" ] && export JAVA_HOME="$(dirname "$(dirname "$JAVA_BIN")")"
fi
command -v javaldx >/dev/null 2>&1 || true

audio_exts="mp3 m4a aac wav flac ogg opus webm amr wma aiff aif"
sheet_exts="xls xlsx ods xlsm xlsb"
doc_exts="doc docx"

# list keys under prefix (uses jq)
list_keys(){ "$AWSCLI" s3api list-objects-v2 --bucket "$S3_BUCKET" --prefix "$S3_RAW_PREFIX" --output json 2>/dev/null | "$JQ_BIN" -r '.Contents[]?.Key' || true; }

s3_cp_down(){ "$AWSCLI" s3 cp "s3://$S3_BUCKET/$1" "$2" 2>/dev/null; }

# uploads and allows optional metadata (comma separated key=value)
s3_upload(){
  local src="$1"; local dst="$2"; local meta="$3"
  if [ -n "$meta" ]; then
    "$AWSCLI" s3 cp "$src" "s3://$S3_BUCKET/$dst" --metadata "$meta" 2>/dev/null
  else
    "$AWSCLI" s3 cp "$src" "s3://$S3_BUCKET/$dst" 2>/dev/null
  fi
}

s3_delete(){ "$AWSCLI" s3 rm "s3://$S3_BUCKET/$1" 2>/dev/null; }

to_lower(){ echo "$1" | tr '[:upper:]' '[:lower:]'; }
basename_no_ext(){ b="$(basename "$1")"; echo "${b%.*}"; }

# return JSON (or empty) for head-object
s3_head_json(){
  if [ -n "${AWS_REGION:-}" ]; then
    "$AWSCLI" s3api head-object --bucket "$S3_BUCKET" --key "$1" --region "$AWS_REGION" --output json 2>/dev/null || echo ""
  else
    "$AWSCLI" s3api head-object --bucket "$S3_BUCKET" --key "$1" --output json 2>/dev/null || echo ""
  fi
}

# parse fields from head json
get_etag_from_head_json(){ echo "$1" | "$JQ_BIN" -r '.ETag // empty' | tr -d '"' || echo ""; }
get_meta_from_head_json(){ echo "$1" | "$JQ_BIN" -r --arg k "$2" '.Metadata[$k] // empty' || echo ""; }
s3_object_exists(){ key="$1"; if [ -n "${AWS_REGION:-}" ]; then "$AWSCLI" s3api head-object --bucket "$S3_BUCKET" --key "$key" --region "$AWS_REGION" >/dev/null 2>&1; else "$AWSCLI" s3api head-object --bucket "$S3_BUCKET" --key "$key" >/dev/null 2>&1; fi; }

process_audio(){
  key="$1"
  name="$(basename "$key")"
  ext="$(to_lower "${name##*.}")"
  [ -z "$(echo " $audio_exts " | grep " $ext ")" ] && return

  # target key: keep consistent folder structure under S3_RAW_PREFIX/audio/
  if [ "$ext" = "wav" ]; then
    s3_target_key="${S3_RAW_PREFIX%/}/audio/${name}"
  else
    s3_target_key="${S3_RAW_PREFIX%/}/audio/$(basename_no_ext "$name").wav"
  fi

  # quick skip: if not overwriting, and target already exists or target==source, skip.
  if [ "${OVERWRITE_ALL_AUDIO_FILES:-false}" != "true" ]; then
    if [ "$s3_target_key" = "$key" ]; then
      # already placed under audio/ as wav; skip
      echo "SKIP: already audio wav key (no overwrite): $key" >&2
      return
    fi
    if s3_object_exists "$s3_target_key"; then
      echo "SKIP: target exists and OVERWRITE_ALL_AUDIO_FILES!=true: s3://$S3_BUCKET/$s3_target_key" >&2
      return
    fi
  fi

  # fetch head JSON for source and target to drive idempotency decisions
  src_head="$(s3_head_json "$key")"
  src_etag="$(get_etag_from_head_json "$src_head")"
  tgt_head="$(s3_head_json "$s3_target_key")"
  tgt_exists="false"
  if [ -n "$tgt_head" ]; then tgt_exists="true"; fi
  tgt_meta_converted_from="$(get_meta_from_head_json "$tgt_head" "converted-from")"
  tgt_meta_converted_etag="$(get_meta_from_head_json "$tgt_head" "converted-etag")"
  tgt_meta_sr="$(get_meta_from_head_json "$tgt_head" "converted-sr")"
  tgt_meta_ch="$(get_meta_from_head_json "$tgt_head" "converted-ch")"

  # If target exists and metadata indicates it was converted from this exact source (same etag), skip.
  if [ "$tgt_exists" = "true" ] && [ -n "$tgt_meta_converted_from" ] && [ "$tgt_meta_converted_from" = "$key" ] && [ -n "$tgt_meta_converted_etag" ] && [ "$tgt_meta_converted_etag" = "$src_etag" ]; then
    echo "SKIP: target already converted from same source (etag match): s3://$S3_BUCKET/$s3_target_key" >&2
    return
  fi

  # If source is already a WAV and target==source, and target metadata indicates correct SR/CH, skip.
  if [ "$ext" = "wav" ] && [ "$s3_target_key" = "$key" ] && [ -n "$tgt_meta_sr" ] && [ -n "$tgt_meta_ch" ]; then
    if [ "$tgt_meta_sr" = "16000" ] && [ "$tgt_meta_ch" = "1" ] && [ "${OVERWRITE_ALL_AUDIO_FILES:-false}" != "true" ]; then
      echo "SKIP: existing WAV already has metadata indicating 16k mono: s3://$S3_BUCKET/$key" >&2
      return
    fi
  fi

  # Otherwise we need to download and maybe convert.
  local_src="$TMP_DIR/src/$name"
  local_out="$TMP_DIR/out/$(basename_no_ext "$name").wav"

  echo "DOWNLOAD: s3://$S3_BUCKET/$key -> $local_src" >&2
  s3_cp_down "$key" "$local_src" || { echo "ERROR: failed to download $key" >&2; rm -f "$local_src"; return; }

  # if source is WAV, inspect sample rate and channels; if already 16k/1 then either copy or (if target==source) skip upload
  if [ "$ext" = "wav" ]; then
    if command -v ffprobe >/dev/null 2>&1; then
      sr=$(ffprobe -v error -select_streams a:0 -show_entries stream=sample_rate -of default=noprint_wrappers=1:nokey=1 "$local_src" 2>/dev/null || echo "")
      ch=$(ffprobe -v error -select_streams a:0 -show_entries stream=channels -of default=noprint_wrappers=1:nokey=1 "$local_src" 2>/dev/null || echo "")
    else
      sr=""; ch=""
    fi

    if [ "${sr:-}" = "16000" ] && [ "${ch:-}" = "1" ]; then
      # already in desired format
      if [ "$s3_target_key" = "$key" ]; then
        # source is target and correctly formatted -> nothing to do (idempotent)
        echo "NOOP: source WAV already 16k mono and is the target: $key" >&2
        rm -f "$local_src"
        return
      fi
      # else copy source to local_out and upload to target (but check if target etag equals src_etag to avoid overwrite)
      cp -f "$local_src" "$local_out"
      # if target exists and metadata indicates same src etag, skip upload (already handled above). otherwise upload
      meta="converted-from=${key},converted-etag=${src_etag},converted-sr=16000,converted-ch=1"
      echo "UPLOAD: $local_out -> s3://$S3_BUCKET/$s3_target_key (converted-from metadata will be set)" >&2
      s3_upload "$local_out" "$s3_target_key" "$meta" || { echo "ERROR: upload failed for $s3_target_key" >&2; rm -f "$local_src" "$local_out"; return; }
      # optionally delete original if overwrite flag says so and keys differ
      if [ "${OVERWRITE_ALL_AUDIO_FILES}" = "true" ] && [ "$s3_target_key" != "$key" ]; then
        s3_delete "$key"
      fi
      rm -f "$local_src" "$local_out"
      return
    fi
  fi

  # Not a pre-formatted wav -> run ffmpeg conversion to 16k mono s16
  tmp_out="$(mktemp "$TMP_DIR/out/tmpout.XXXXXX.wav")"
  echo "CONVERT: $local_src -> $tmp_out (ffmpeg -ar 16000 -ac 1 -sample_fmt s16)" >&2
  "$FFMPEG_BIN" -y -hide_banner -loglevel error -i "$local_src" -ar 16000 -ac 1 -sample_fmt s16 "$tmp_out" || { echo "ERROR: ffmpeg failed on $local_src" >&2; rm -f "$local_src" "$tmp_out"; return; }
  mv -f "$tmp_out" "$local_out"

  # upload with conversion metadata for future idempotency checks
  meta="converted-from=${key},converted-etag=${src_etag},converted-sr=16000,converted-ch=1"
  echo "UPLOAD: $local_out -> s3://$S3_BUCKET/$s3_target_key (setting metadata)" >&2
  s3_upload "$local_out" "$s3_target_key" "$meta" || { echo "ERROR: upload failed for $s3_target_key" >&2; rm -f "$local_src" "$local_out"; return; }

  # optionally delete original source if overwrite requested and keys differ
  if [ "${OVERWRITE_ALL_AUDIO_FILES}" = "true" ] && [ "$s3_target_key" != "$key" ]; then
    s3_delete "$key"
  fi

  rm -f "$local_src" "$local_out"
}

process_doc(){
  key="$1"
  name="$(basename "$key")"
  ext="$(to_lower "${name##*.}")"
  [ -z "$(echo " $doc_exts " | grep " $ext ")" ] && return
  local_src="$TMP_DIR/src/$name"
  s3_cp_down "$key" "$local_src" || return
  "$SOFFICE_BIN" --headless --invisible --nologo --nodefault --nofirststartwizard --nolockcheck --convert-to pdf:writer_pdf_Export --outdir "$TMP_DIR/out" "$local_src"
  out_pdf="$TMP_DIR/out/$(basename_no_ext "$name").pdf"
  [ ! -f "$out_pdf" ] && for f in "$TMP_DIR/out"/*.pdf; do [ -f "$f" ] && out_pdf="$f" && break; done
  if [ -f "$out_pdf" ]; then
    s3_target_key="${S3_RAW_PREFIX%/}/pdfs/${name}.pdf"
    s3_upload "$out_pdf" "$s3_target_key"
    [ "${OVERWRITE_OTHER_TO_PDF}" = "true" ] && s3_delete "$key"
  fi
  rm -f "$local_src" "$out_pdf"
}

process_sheet(){
  key="$1"
  name="$(basename "$key")"
  ext="$(to_lower "${name##*.}")"
  [ -z "$(echo " $sheet_exts " | grep " $ext ")" ] && return
  local_src="$TMP_DIR/src/$name"
  s3_cp_down "$key" "$local_src" || return
  "$SOFFICE_BIN" --headless --invisible --nologo --nodefault --nofirststartwizard --nolockcheck --convert-to csv --outdir "$TMP_DIR/out" "$local_src"
  shopt -s nullglob
  for f in "$TMP_DIR/out"/*.csv; do
    base_csv="$(basename "$f")"
    s3_upload "$f" "${S3_RAW_PREFIX%/}/csvs/${name}.${base_csv}"
  done
  [ "${OVERWRITE_SPREADSHEETS_WITH_CSV}" = "true" ] && s3_delete "$key"
  rm -f "$local_src" "$TMP_DIR/out"/*.csv
}

group_remaining(){
  list_keys | while IFS= read -r key; do
    [[ -z "$key" ]] && continue
    [[ "$key" == */ ]] && continue
    [[ "$key" == *.manifest.json ]] && continue
    name="$(basename "$key")"
    ext="$(to_lower "${name##*.}")"
    case "$ext" in
      mp3|m4a|aac|wav|flac|ogg|opus|webm|amr|wma|aiff|aif) sub="audio/";;
      jpg|jpeg|png|webp|tif|tiff|bmp|gif) sub="images/";;
      pdf) sub="pdfs/";;
      doc|docx) sub="docs/";;
      ppt|pptx) sub="ppts/";;
      txt) sub="txts/";;
      csv) sub="csvs/";;
      md) sub="mds/";;
      html) sub="htmls/";;
      jsonl) sub="jsonls/";;
      *) sub="others/";;
    esac
    dst="${S3_RAW_PREFIX}${sub}${name}"
    [ "$dst" != "$key" ] && "$AWSCLI" s3 mv "s3://$S3_BUCKET/$key" "s3://$S3_BUCKET/$dst" || true
  done
}

cleanup(){ rm -rf "$TMP_DIR/src" "$TMP_DIR/out" || true; }
trap cleanup EXIT

main_loop(){
  list_keys | while IFS= read -r key; do
    [[ -z "$key" ]] && continue
    [[ "$key" == */ ]] && continue
    [[ "$key" == *.manifest.json ]] && continue
    ext="$(to_lower "${key##*.}")"
    if echo " $audio_exts " | grep -q " $ext "; then
      process_audio "$key"
    elif echo " $doc_exts " | grep -q " $ext "; then
      process_doc "$key"
    elif echo " $sheet_exts " | grep -q " $ext "; then
      process_sheet "$key"
    fi
  done
  group_remaining
}

main_loop