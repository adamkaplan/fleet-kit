#!/bin/sh
# A stand-in opencode for the entrypoint tests: records its arguments, and for `auth import` the stdin it was given.
# FAKE_OC_FAIL makes the import fail. It writes a marker (not the credential) into the data directory, as the real one writes its database.
rec="${FAKE_OC_REC:?}"
echo "$*" >> "$rec/calls"
[ "${1:-} ${2:-}" = "auth import" ] || exit 9
cat > "$rec/stdin"
# FAKE_OC_ECHO makes it print what it was given on stdout AND stderr (what a leaky import would show), on success and on failure.
if [ -n "${FAKE_OC_ECHO:-}" ]; then cat "$rec/stdin"; cat "$rec/stdin" >&2; fi
[ -z "${FAKE_OC_FAIL:-}" ] || exit 1
echo imported > "${XDG_DATA_HOME:?}/opencode/store"
echo import >> "$rec/order"
# FAKE_OC_STORE: write the credential into the store, as the real server does (its database); a recognisable value for the disk scan
[ -z "${FAKE_OC_STORE:-}" ] || cat "$rec/stdin" > "${XDG_DATA_HOME}/opencode/opencode.db"
