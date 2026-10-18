#!/usr/bin/env bash
# Build-failing scan for stored credentials: files (binary included, via scan-files.py) and the image config
# (env, history). Prints "path: pattern-name" per hit, never the value.
# Usage: scan-secrets.sh <image-ref> | --archive <file.tar> | --dir <dir> [--skip <name>]... | --meta <image-ref>
# Exit 0 = 0 hits, 1 = hits, 2 = usage/error/not readable (never a pass).
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# name@@ERE, one per line. Values are never printed.
read -r -d '' PATTERNS <<'P' || true
github-token@@(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})
aws-access-key@@(AKIA|ASIA)[0-9A-Z]{16}
private-key@@^-----BEGIN ([A-Z]+ )?PRIVATE KEY-----$
azure-client-secret@@(AZURE_CLIENT_SECRET|ARM_CLIENT_SECRET)["']?[=:] *["']?[^ "'$]{4,}
opencode-password@@OPENCODE_SERVER_PASSWORD["']?[=:] *["']?[^ "'$]{1,}
generic-token-assignment@@(GH_TOKEN|GITHUB_TOKEN|API_KEY|ACCESS_TOKEN)["']?[=:] *["']?[A-Za-z0-9_./+-]{16,}
jwt@@eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}
P

SKIPS=()   # top-level names of --dir that are not scanned (--skip)
# Roots to scan: the children of $1 that are real (not symlinks) and not skipped. Skips are exact top-level
# names, never a name matched at any depth (a directory called "proc" deeper down IS scanned).
roots_of() {
  local dir=${1%/} p b s skip
  while IFS= read -r -d '' p; do
    b=${p##*/}; skip=0
    for s in ${SKIPS[@]+"${SKIPS[@]}"}; do [ "$b" = "$s" ] && skip=1; done
    [ "$skip" = 1 ] || printf '%s\0' "$p"
  done < <(find "${dir:-/}" -mindepth 1 -maxdepth 1 ! -type l -print0)
}

scan_dir() {
  local dir=$1 hits=0 out rc f p
  local -a roots=()
  while IFS= read -r -d '' p; do roots+=("$p"); done < <(roots_of "$dir")
  [ ${#roots[@]} -gt 0 ] || { echo "scan-secrets: nothing to scan under $dir: not scanned" >&2; exit 2; }
  local prefix="${dir%/}/" pf
  # the files are read as bytes by scan-files.py (binary, UTF-16 and NUL-padded files included); an error is exit 2
  pf=$(mktemp); printf '%s\n' "$PATTERNS" > "$pf"
  rc=0; out=$(python3 "$HERE/scan-files.py" "$pf" "${roots[@]}" 2>/dev/null) || rc=$?
  rm -f "$pf"
  [ "$rc" -le 1 ] || { echo "scan-secrets: could not read some files under $dir: not scanned" >&2; exit 2; }
  if [ -n "$out" ]; then
    while IFS= read -r f; do echo "${f#"$prefix"}"; hits=$((hits + 1)); done <<<"$out"
  fi
  # an ssh key or credential file by name (an unreadable directory is an error, as above)
  rc=0; out=$(find "${roots[@]}" \( -path '*/.ssh/id_*' -o -name '.git-credentials' -o -name '.netrc' -o -path '*/.config/gh/hosts.yml' -o -name 'kubeconfig' \) ! -name '*.pub' -type f 2>/dev/null) || rc=$?
  [ "$rc" -eq 0 ] || { echo "scan-secrets: could not read some files under $dir: not scanned" >&2; exit 2; }
  if [ -n "$out" ]; then
    while IFS= read -r f; do echo "${f#"$prefix"}: credential-file"; hits=$((hits + 1)); done <<<"$out"
  fi
  return $((hits > 0))
}

report() { # <what was scanned> <command...>
  local what=$1; shift
  if "$@"; then echo "scan-secrets: 0 hits$what"; exit 0; else echo "scan-secrets: hits found$what" >&2; exit 1; fi
}

# --dir <dir> [--skip <top-level name>]...: a directory tree (the build runs it with --dir / inside the image).
if [ "${1:-}" = "--dir" ] && [ -n "${2:-}" ]; then
  d=$2; shift 2
  while [ $# -gt 0 ]; do
    [ "$1" = "--skip" ] && [ -n "${2:-}" ] || { echo "usage: $0 --dir <dir> [--skip <name>]..." >&2; exit 2; }
    SKIPS+=("$2"); shift 2
  done
  report "" scan_dir "$d"
fi
# --meta <image>: the image configuration (env, labels, cmd) and history, with the same patterns. It reads no layer,
# so it works on any workstation; it does not replace the layer scan the build runs.
if [ "${1:-}" = "--meta" ] && [ -n "${2:-}" ] && [ $# -eq 2 ]; then
  work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
  docker image inspect --format '{{json .Config}}' "$2" > "$work/config.json" 2>/dev/null && [ -s "$work/config.json" ] \
    || { echo "scan-secrets: cannot inspect $2: not scanned" >&2; exit 2; }
  docker history --no-trunc --format '{{.CreatedBy}}' "$2" > "$work/history.txt" 2>/dev/null && [ -s "$work/history.txt" ] \
    || { echo "scan-secrets: cannot read the history of $2: not scanned" >&2; exit 2; }
  report " (META SCAN ONLY: image configuration and history; layer FILES NOT SCANNED)" scan_dir "$work"
fi
# --archive <file>: an image archive (docker save or OCI layout), scanned layer by layer (THE gate; see README).
archive_scan() {
  local pf rc=0; pf=$(mktemp); printf '%s\n' "$PATTERNS" > "$pf"
  python3 "$HERE/scan-layers.py" "$pf" "$1" || rc=$?
  rm -f "$pf"; exit "$rc"
}
if [ "${1:-}" = "--archive" ] && [ -n "${2:-}" ] && [ $# -eq 2 ]; then
  [ -f "$2" ] || { echo "scan-secrets: no such archive $2: not scanned" >&2; exit 2; }
  archive_scan "$2"
fi
[ $# -eq 1 ] || { echo "usage: $0 <image> | --archive <file.tar> | --dir <dir> [--skip <name>]... | --meta <image>" >&2; exit 2; }

work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
docker save "$1" -o "$work/image.tar" 2>/dev/null || { echo "scan-secrets: cannot export $1: not scanned" >&2; exit 2; }
archive_scan "$work/image.tar"
