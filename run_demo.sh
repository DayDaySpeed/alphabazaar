#!/usr/bin/env bash
# AlphaBazaar one-command demo: start both seller agents, run the analyst, stop.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
FUNDING_PORT="${FUNDING_PORT:-8801}"
RISK_PORT="${RISK_PORT:-8802}"

echo "▶ starting seller agents…"
$PY -m uvicorn sellers.funding_scanner:app --host 127.0.0.1 --port "$FUNDING_PORT" --log-level warning &
FUNDING_PID=$!
$PY -m uvicorn sellers.risk_analyzer:app --host 127.0.0.1 --port "$RISK_PORT" --log-level warning &
RISK_PID=$!

cleanup() {
  echo
  echo "▶ stopping seller agents…"
  kill "$FUNDING_PID" "$RISK_PID" 2>/dev/null || true
  wait "$FUNDING_PID" "$RISK_PID" 2>/dev/null || true
}
trap cleanup EXIT

# wait for both to answer
for port in "$FUNDING_PORT" "$RISK_PORT"; do
  for i in $(seq 1 40); do
    if curl -sf "http://127.0.0.1:${port}/" >/dev/null 2>&1; then break; fi
    sleep 0.25
    if [ "$i" -eq 40 ]; then echo "seller on :$port did not start"; exit 1; fi
  done
done
echo "▶ sellers up on :$FUNDING_PORT and :$RISK_PORT"
echo

SELLER_FUNDING_URL="http://127.0.0.1:${FUNDING_PORT}" \
SELLER_RISK_URL="http://127.0.0.1:${RISK_PORT}" \
  $PY -m alphabazaar.cli run "$@"
