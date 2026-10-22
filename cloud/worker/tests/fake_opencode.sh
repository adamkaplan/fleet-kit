#!/bin/sh
# A stand-in opencode for the entrypoint tests: records its arguments, and for `auth import` the stdin it was given.
# FAKE_OC_FAIL makes the import fail. It writes a marker (not the credential) into the data directory, as the real one writes its database.
rec="${FAKE_OC_REC:?}"
echo "$*" >> "$rec/calls"
[ "${1:-} ${2:-}" = "auth import" ] || exit 9
cat > "$rec/stdin"
[ -z "${FAKE_OC_FAIL:-}" ] || exit 1
echo imported > "${XDG_DATA_HOME:?}/opencode/store"
echo import >> "$rec/order"
