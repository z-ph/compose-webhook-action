#!/usr/bin/env bash
set -euo pipefail

url="${WEBHOOK_URL%/}"
if [[ "$url" != */hook ]]; then
  url="${url}/hook"
fi
base="${url%/hook}"
timeout="${WEBHOOK_TIMEOUT:-30}"
wait_flag="${WEBHOOK_WAIT:-false}"
wait_timeout="${WEBHOOK_WAIT_TIMEOUT:-1800}"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

http_code="$(
  curl -sS -o "$tmp" -w '%{http_code}' \
    --max-time "$timeout" \
    -X POST "$url" \
    -H "Authorization: Bearer ${WEBHOOK_TOKEN}" \
    -H "Content-Type: application/json" \
    --data '{}'
)"

body="$(cat "$tmp")"
echo "hook HTTP ${http_code}"
echo "$body"

if [[ "$http_code" != "202" ]]; then
  echo "::error::webhook rejected request (HTTP ${http_code})"
  exit 1
fi

job_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("job_id",""))' <<<"$body")"
if [[ -z "$job_id" ]]; then
  echo "::error::response missing job_id"
  exit 1
fi

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "job-id=${job_id}" >> "$GITHUB_OUTPUT"
  echo "http-code=${http_code}" >> "$GITHUB_OUTPUT"
fi

if [[ "$wait_flag" != "true" ]]; then
  echo "accepted job ${job_id} (not waiting)"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "status=accepted" >> "$GITHUB_OUTPUT"
  fi
  exit 0
fi

deadline=$((SECONDS + wait_timeout))
status="unknown"
while (( SECONDS < deadline )); do
  curl -sS -o "$tmp" --max-time 15 \
    -H "Authorization: Bearer ${WEBHOOK_TOKEN}" \
    "${base}/status"
  status="$(python3 -c 'import json,sys; j=json.load(sys.stdin).get("job") or {}; print(j.get("status",""))' <"$tmp")"
  echo "status=${status}"
  case "$status" in
    ok)
      if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
        echo "status=ok" >> "$GITHUB_OUTPUT"
      fi
      exit 0
      ;;
    fail)
      echo "::error::compose job ${job_id} failed"
      if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
        echo "status=fail" >> "$GITHUB_OUTPUT"
      fi
      exit 1
      ;;
  esac
  sleep 5
done

echo "::error::timed out waiting for job ${job_id} after ${wait_timeout}s (last status=${status})"
exit 1
