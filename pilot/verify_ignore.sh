#!/usr/bin/env bash
# Verify the ignore list end-to-end against a running instance.
#   ./verify_ignore.sh <base-url> <device_id>
# Uses a cookie jar + a token extracted with sed, avoiding shell quoting
# problems that repeatedly produced spurious CSRF 400s.
set -euo pipefail

BASE="${1:-http://localhost:5060}"
DEV="${2:-pt100-3}"
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

login() {
    curl -s -c "$JAR" -b "$JAR" -X POST "$BASE/login" \
        -d "username=yaniv&password=mushroom" -o /dev/null
}

token() {
    curl -s -c "$JAR" -b "$JAR" "$BASE/admin/ignored" \
        | grep -o 'name="_csrf" value="[a-f0-9]*"' | head -1 \
        | sed 's/.*value="//; s/"$//'
}

post() {
    local action="$1"
    curl -s -c "$JAR" -b "$JAR" -X POST "$BASE/admin/ignored" \
        --data-urlencode "_csrf=$(token)" \
        --data-urlencode "action=$action" \
        --data-urlencode "device_id=$DEV" \
        -o /dev/null -w "%{http_code}"
}

count_devices() {
    curl -s -b "$JAR" "$BASE/api/devices" \
        | python3 -c "import json,sys; print(len(json.load(sys.stdin)['devices']))"
}

is_present() {
    curl -s -b "$JAR" "$BASE/api/devices" \
        | python3 -c "
import json,sys
ids=[d['device_id'] for d in json.load(sys.stdin)['devices']]
print('true' if '$DEV' in ids else 'false')"
}

login
echo "  baseline: $DEV present = $(is_present), device count = $(count_devices)"

echo "  ignore  -> HTTP $(post add)"
sleep 4
echo "  after ignore: present = $(is_present), device count = $(count_devices)"

echo "  remove  -> HTTP $(post remove)"
sleep 4
echo "  after remove: present = $(is_present), device count = $(count_devices)"
