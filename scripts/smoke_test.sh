#!/usr/bin/env bash
# Post-deployment checks against a live URL, e.g. scripts/smoke_test.sh https://staging.example.com
set -euo pipefail

BASE_URL="${1:?usage: smoke_test.sh https://host}"
BASE_URL="${BASE_URL%/}"
HOST="${BASE_URL#https://}"

for _ in $(seq 1 30); do
  curl -fsS --max-time 10 "$BASE_URL/healthz/" >/dev/null && break
  sleep 5
done

expect() {
  local url=$1 expected=$2 code
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$url")
  if [[ ! "$code" =~ ^($expected)$ ]]; then
    echo "FAIL $url -> $code (expected $expected)"
    exit 1
  fi
  echo "ok   $url -> $code"
}

expect "$BASE_URL/healthz/" 200
expect "$BASE_URL/accounts/login/" 200
expect "$BASE_URL/admin/login/" 200
expect "$BASE_URL/" 302
expect "$BASE_URL/reports/" 302
expect "$BASE_URL/documents/upload/" 302
expect "http://$HOST/accounts/login/" "301|302|307|308"

headers=$(curl -sSI --max-time 20 "$BASE_URL/accounts/login/")
grep -qi '^strict-transport-security:' <<<"$headers" || { echo "FAIL missing HSTS header"; exit 1; }
grep -qi '^x-frame-options: *deny' <<<"$headers" || { echo "FAIL missing X-Frame-Options"; exit 1; }
grep -qi '^set-cookie:.*csrftoken=.*secure' <<<"$headers" || { echo "FAIL CSRF cookie not Secure"; exit 1; }
echo "Smoke tests passed for $BASE_URL"
