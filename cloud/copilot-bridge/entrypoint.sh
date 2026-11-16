#!/bin/bash
# Entrypoint of the Copilot bridge pod (ask #2341). No secret is in the image: the owner's GitHub token is a Secret file mounted
# read-only at /run/copilot/github_token. Step 1 (init) copies it into the in-memory home (HOME=/run/home, an emptyDir with medium
# Memory) at the place copilot-api reads it, mode 0600. Step 2 starts the bridge on the loopback only. Step 3 starts the policy front.
# copilot-api 0.7.0 has NO bind-address option and binds every interface by default; its HTTP server (srvx) reads the HOST
# environment variable, so HOST=127.0.0.1 is set for the bridge only. --show-token is never passed.
set -eu
umask 077
SRC=/run/copilot/github_token
DIR="${HOME:?HOME must be the in-memory home}/.local/share/copilot-api"
RATE="${BRIDGE_RATE_LIMIT_SECONDS:-2}"
case "$RATE" in ''|*[!0-9]*) echo "BRIDGE_RATE_LIMIT_SECONDS must be a whole number of seconds" >&2; exit 2 ;; esac
[ -s "$SRC" ] || { echo "the token file $SRC is missing or empty" >&2; exit 2; }
mkdir -p "$DIR"
install -m 0600 "$SRC" "$DIR/github_token"
cd /app
HOST=127.0.0.1 node node_modules/copilot-api/dist/main.js start --port 4141 --rate-limit "$RATE" --wait &
bridge=$!
node bridge-front.mjs &
front=$!
trap 'kill "$bridge" "$front" 2>/dev/null || true' TERM INT
wait -n "$bridge" "$front" || true
kill "$bridge" "$front" 2>/dev/null || true
exit 1
