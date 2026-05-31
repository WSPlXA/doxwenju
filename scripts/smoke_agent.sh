#!/usr/bin/env bash
set -euo pipefail

API_URL="${API_URL:-http://localhost:8000}"
TEMPLATE_DOCX="${TEMPLATE_DOCX:-agent-output-full-layout-v6.docx}"
TARGET_DOCX="${TARGET_DOCX:-agent-output-inline-word-final.docx}"
MAX_ROUNDS="${MAX_ROUNDS:-2}"
POLL_SECONDS="${POLL_SECONDS:-2}"
POLL_ATTEMPTS="${POLL_ATTEMPTS:-90}"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/doxwenju-smoke}"

mkdir -p "$OUTPUT_DIR"

require_file() {
  if [[ ! -f "$1" ]]; then
    echo "Missing file: $1" >&2
    exit 1
  fi
}

json_field() {
  python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$1"
}

poll_status() {
  local url="$1"
  local label="$2"
  local body state
  for _ in $(seq 1 "$POLL_ATTEMPTS"); do
    body="$(curl -fsS "$url")"
    state="$(printf '%s' "$body" | json_field status)"
    echo "$label status: $state" >&2
    if [[ "$state" == "done" || "$state" == "failed" || "$state" == "error" || "$state" == "needs_human" ]]; then
      printf '%s\n' "$body"
      return 0
    fi
    sleep "$POLL_SECONDS"
  done
  echo "$label did not finish within $((POLL_SECONDS * POLL_ATTEMPTS))s" >&2
  return 1
}

require_file "$TEMPLATE_DOCX"
require_file "$TARGET_DOCX"

echo "Using API: $API_URL"
echo "Uploading template: $TEMPLATE_DOCX"
curl -fsS -F "file=@${TEMPLATE_DOCX}" "$API_URL/templates/current" >/dev/null
template_status="$(poll_status "$API_URL/templates/current/status" template)"
if [[ "$(printf '%s' "$template_status" | json_field status)" != "done" ]]; then
  echo "Template ingestion did not finish successfully" >&2
  exit 1
fi

echo "Uploading target: $TARGET_DOCX"
curl -fsS -F "file=@${TARGET_DOCX}" "$API_URL/targets" >/dev/null
target_status="$(poll_status "$API_URL/targets/latest/status" target)"
if [[ "$(printf '%s' "$target_status" | json_field status)" != "done" ]]; then
  echo "Target ingestion did not finish successfully" >&2
  exit 1
fi

elements="$(curl -fsS "$API_URL/targets/latest/elements?limit=1")"
mappings="$(curl -fsS "$API_URL/targets/latest/mappings?limit=1")"
plan="$(curl -fsS "$API_URL/targets/latest/patch-plan")"
echo "Target elements: $(printf '%s' "$elements" | json_field total)"
echo "Mapping results: $(printf '%s' "$mappings" | json_field total)"
echo "Patch operations: $(printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin)["summary"].get("operationCount", ""))')"

echo "Starting agent run, max rounds: $MAX_ROUNDS"
curl -fsS -X POST "$API_URL/targets/latest/agent-run?max_rounds=${MAX_ROUNDS}" >/dev/null
run_status="$(poll_status "$API_URL/targets/latest/agent-run/status" agent)"
run_state="$(printf '%s' "$run_status" | json_field status)"

printf '%s' "$run_status" | python3 -c '
import json, sys
run = json.load(sys.stdin)
summary = run.get("summary") or {}
print("Agent status:", run.get("status"))
print("Stop reason:", summary.get("stopReason"))
print("Render gate:", summary.get("renderGateReason"))
print("Rounds:", run.get("round_count"))
print("Skipped operations:", summary.get("skippedOperations"))
print("Page drift:", summary.get("pageCountDrift"))
'

if [[ "$run_state" != "done" && "$run_state" != "needs_human" ]]; then
  echo "Agent run ended unexpectedly: $run_state" >&2
  exit 1
fi

curl -fsS -o "$OUTPUT_DIR/output.docx" "$API_URL/targets/latest/output.docx"
curl -fsS -o "$OUTPUT_DIR/render.pdf" "$API_URL/targets/latest/render.pdf"
echo "Downloaded artifacts to $OUTPUT_DIR"
