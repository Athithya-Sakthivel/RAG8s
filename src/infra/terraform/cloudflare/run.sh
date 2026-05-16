#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

STACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_BIN="${TF_BIN:-tofu}"

usage() {
  cat <<'USAGE'
Usage: run.sh --plan|--apply|--destroy
USAGE
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: missing required command: $1" >&2
    exit 1
  }
}

require_cmd "$TF_BIN"
require_cmd curl
require_cmd jq
require_cmd cloudflared

[ $# -eq 1 ] || usage
MODE="$1"
case "$MODE" in
  --plan|--apply|--destroy) ;;
  *) usage ;;
esac

# Export TF_VAR_domain FIRST before using it
export TF_VAR_domain="${TF_VAR_domain:-${CLOUDFLARE_ZONE:-${DOMAIN:-}}}"
export TF_VAR_account_id="$CLOUDFLARE_ACCOUNT_ID"
# Now TF_VAR_domain is available for use in the curl command
export TF_VAR_zone_id=$(curl -s -H "X-Auth-Key: $CLOUDFLARE_GLOBAL_API_KEY" -H "X-Auth-Email: $CLOUDFLARE_EMAIL" "https://api.cloudflare.com/client/v4/zones?name=${TF_VAR_domain}" | jq -r '.result[0].id')
export TF_VAR_tunnel_name="${TF_VAR_tunnel_name:-${CLOUDFLARE_TUNNEL_NAME:-default-tunnel-1}}"
export TF_VAR_enable_always_use_https="${TF_VAR_enable_always_use_https:-true}"
export TF_VAR_enable_tls_1_3="${TF_VAR_enable_tls_1_3:-true}"
export TF_VAR_enable_bot_fight_mode="${TF_VAR_enable_bot_fight_mode:-false}"
export TF_VAR_enable_js_detections="${TF_VAR_enable_js_detections:-false}"
export TF_IN_AUTOMATION=1
export TF_INPUT=0

: "${TF_VAR_account_id:?TF_VAR_account_id or CLOUDFLARE_ACCOUNT_ID is required}"
: "${TF_VAR_domain:?TF_VAR_domain or DOMAIN is required}"

if [[ -n "${CLOUDFLARE_API_TOKEN:-}" && ( -n "${CLOUDFLARE_API_KEY:-}" || -n "${CLOUDFLARE_GLOBAL_API_KEY:-}" ) ]]; then
  echo "ERROR: set either CLOUDFLARE_API_TOKEN or CLOUDFLARE_GLOBAL_API_KEY/CLOUDFLARE_API_KEY, not both" >&2
  exit 2
fi

if [[ -n "${CLOUDFLARE_API_TOKEN:-}" ]]; then
  export CLOUDFLARE_API_TOKEN
  unset CLOUDFLARE_API_KEY
  unset CLOUDFLARE_GLOBAL_API_KEY
  unset CLOUDFLARE_EMAIL
else
  export CLOUDFLARE_API_KEY="${CLOUDFLARE_API_KEY:-${CLOUDFLARE_GLOBAL_API_KEY:-}}"
  : "${CLOUDFLARE_API_KEY:?set CLOUDFLARE_API_TOKEN or CLOUDFLARE_GLOBAL_API_KEY}"
  : "${CLOUDFLARE_EMAIL:?CLOUDFLARE_EMAIL is required with a global API key}"
  export CLOUDFLARE_API_KEY
  export CLOUDFLARE_EMAIL
  unset CLOUDFLARE_API_TOKEN
fi

cf_headers() {
  if [[ -n "${CLOUDFLARE_API_TOKEN:-}" ]]; then
    printf '%s\n' -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}"
  else
    printf '%s\n' -H "X-Auth-Key: ${CLOUDFLARE_API_KEY}" -H "X-Auth-Email: ${CLOUDFLARE_EMAIL}"
  fi
}

cf_curl() {
  local -a args=()
  while IFS= read -r line; do
    args+=("$line")
  done < <(cf_headers)
  curl -fsS "${args[@]}" "$@"
}

cf_status() {
  local -a args=()
  while IFS= read -r line; do
    args+=("$line")
  done < <(cf_headers)
  curl -sS -o /dev/null -w '%{http_code}' "${args[@]}" "$1"
}

resolve_zone_id() {
  if [[ -n "${TF_VAR_zone_id:-}" && "${TF_VAR_zone_id}" != "your_zone_id" && "${TF_VAR_zone_id}" != "your_real_zone_id" && "${TF_VAR_zone_id}" != "replace-me" && "${TF_VAR_zone_id}" != "null" ]]; then
    return 0
  fi

  echo "[INFO] resolving zone_id for ${TF_VAR_domain}" >&2
  local zone_json
  zone_json="$(cf_curl "https://api.cloudflare.com/client/v4/zones?name=${TF_VAR_domain}&status=active&per_page=1")"
  TF_VAR_zone_id="$(jq -r '.result[0].id // empty' <<<"${zone_json}")"
  if [[ -z "${TF_VAR_zone_id}" ]]; then
    echo "ERROR: failed to resolve zone_id for ${TF_VAR_domain}" >&2
    exit 3
  fi
  export TF_VAR_zone_id
  echo "[INFO] zone_id=${TF_VAR_zone_id}" >&2
}

ensure_cloudflared_login() {
  if [[ ! -f "${HOME}/.cloudflared/cert.pem" ]]; then
    echo "[INFO] cloudflared login required" >&2
    cloudflared tunnel login >&2
  fi
}

get_tunnel_id() {
  cloudflared tunnel list --output json \
    | jq -r --arg n "${TF_VAR_tunnel_name}" '.[] | select(.name == $n) | .id' \
    | head -n1
}

ensure_tunnel() {
  ensure_cloudflared_login

  local tunnel_id=""
  tunnel_id="$(get_tunnel_id || true)"
  if [[ -z "${tunnel_id}" || "${tunnel_id}" == "null" ]]; then
    echo "[INFO] creating tunnel ${TF_VAR_tunnel_name}" >&2
    cloudflared tunnel create "${TF_VAR_tunnel_name}" >&2 || true
  else
    echo "[INFO] reusing tunnel ${TF_VAR_tunnel_name}" >&2
  fi

  for _ in $(seq 1 10); do
    tunnel_id="$(get_tunnel_id || true)"
    if [[ -n "${tunnel_id}" && "${tunnel_id}" != "null" ]]; then
      echo "${tunnel_id}"
      return 0
    fi
    sleep 1
  done

  echo "ERROR: could not resolve tunnel ID" >&2
  exit 5
}

import_if_exists() {
  local addr="$1"
  local import_id="$2"

  if "$TF_BIN" -chdir="${STACK_DIR}" state show "${addr}" >/dev/null 2>&1; then
    return 0
  fi

  echo "[INFO] importing ${addr}" >&2
  "$TF_BIN" -chdir="${STACK_DIR}" import -input=false "${addr}" "${import_id}"
}

import_dns_record_if_exists() {
  local addr="$1"
  local host="$2"

  local record_json record_id
  record_json="$(cf_curl "https://api.cloudflare.com/client/v4/zones/${TF_VAR_zone_id}/dns_records?name=${host}&type=CNAME")"
  record_id="$(jq -r '.result[0].id // empty' <<<"${record_json}")"
  if [[ -n "${record_id}" ]]; then
    import_if_exists "${addr}" "${TF_VAR_zone_id}/${record_id}"
  fi
}

import_zone_setting_if_exists() {
  local addr="$1"
  local setting_id="$2"
  import_if_exists "${addr}" "${TF_VAR_zone_id}/${setting_id}"
}

import_bot_management_if_exists() {
  local status
  status="$(cf_status "https://api.cloudflare.com/client/v4/zones/${TF_VAR_zone_id}/bot_management")"
  if [[ "${status}" == "200" ]]; then
    import_if_exists "cloudflare_bot_management.zone" "${TF_VAR_zone_id}"
  fi
}

cleanup_tunnel() {
  local tunnel_id
  tunnel_id="$(get_tunnel_id || true)"

  if [[ -n "${tunnel_id}" && "${tunnel_id}" != "null" ]]; then
    echo "[INFO] deleting tunnel ${TF_VAR_tunnel_name} (${tunnel_id})" >&2
    cloudflared tunnel delete -f "${tunnel_id}" >/dev/null || true
  fi
}

resolve_zone_id

if [[ "${MODE}" != "--destroy" ]]; then
  TUNNEL_ID="$(ensure_tunnel)"
  export TUNNEL_ID
fi

"$TF_BIN" -chdir="${STACK_DIR}" init -input=false -upgrade
"$TF_BIN" -chdir="${STACK_DIR}" validate

if [[ "${MODE}" != "--destroy" ]]; then
  # Import root domain CNAME
  import_dns_record_if_exists "cloudflare_dns_record.root_cname" "${TF_VAR_domain}"
  # Import wildcard CNAME (optional - remove if not using wildcard)
  import_dns_record_if_exists "cloudflare_dns_record.wildcard_cname" "*.${TF_VAR_domain}"
  import_zone_setting_if_exists "cloudflare_zone_setting.ssl" "ssl"
  import_zone_setting_if_exists "cloudflare_zone_setting.always_use_https[0]" "always_use_https"
  import_zone_setting_if_exists "cloudflare_zone_setting.tls_1_3[0]" "tls_1_3"
  import_bot_management_if_exists
fi

case "${MODE}" in
  --plan)
    "$TF_BIN" -chdir="${STACK_DIR}" plan -input=false -out=tfplan
    ;;
  --apply)
    "$TF_BIN" -chdir="${STACK_DIR}" apply -input=false -auto-approve
    "$TF_BIN" -chdir="${STACK_DIR}" output
    ;;
  --destroy)
    "$TF_BIN" -chdir="${STACK_DIR}" destroy -input=false -auto-approve
    cleanup_tunnel
    ;;
esac