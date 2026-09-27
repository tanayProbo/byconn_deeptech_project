#!/usr/bin/env bash
# Starts the datastores and the BYCONN-X server for a live demo, then waits
# until /api/v1/health reports every dependency up.
#
#   scripts/demo_up.sh            # uses .env for the LLM settings
#   PORT=8080 scripts/demo_up.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-8000}"
HEALTH="http://localhost:${PORT}/api/v1/health"

command -v docker >/dev/null || { echo "docker is required for the datastores" >&2; exit 1; }
command -v byconn-server >/dev/null || { echo "run: pip install -e . (provides byconn-server)" >&2; exit 1; }

echo "Starting PostgreSQL, Qdrant and Neo4j..."
docker compose up -d

echo "Starting the server on port ${PORT}..."
PORT="$PORT" byconn-server &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' INT TERM

# Neo4j takes the longest to accept connections; allow up to two minutes.
for _ in $(seq 1 120); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH" || true)
    if [ "$code" = "200" ]; then
        echo "All dependencies are up."
        curl -s "$HEALTH" | python3 -m json.tool
        echo
        echo "Dashboard: http://localhost:${PORT}/dashboard/"
        wait "$SERVER_PID"
        exit 0
    fi
    sleep 1
done

echo "Health is still not 200 after 120s. Current report:" >&2
curl -s "$HEALTH" | python3 -m json.tool >&2 || true
echo "The server is still running (pid $SERVER_PID); check docker compose ps." >&2
wait "$SERVER_PID"
