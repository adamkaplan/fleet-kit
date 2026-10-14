#!/usr/bin/env bash
# Build-failing scan for stored credentials in a built image: every layer's files and
# the image config (env, history). Prints "path: pattern-name" per hit, never the value.
# Usage: scan-secrets.sh <image-ref | --dir <extracted-dir>>   Exit 0 = 0 hits, 1 = hits, 2 = usage/error.
set -euo pipefail

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

scan_dir() {
  local dir=$1 hits=0 name re out
  while IFS= read -r line; do
    name=${line%%@@*}; re=${line#*@@}
    rc=0; out=$(grep -rIlE --exclude-dir=proc --exclude-dir=sys -e "$re" "$dir" 2>/dev/null) || rc=$?
    # 0 = hits, 1 = none; anything else means files were not read: an error, never a pass
    [ "$rc" -le 1 ] || { echo "scan-secrets: could not read some files under $dir: not scanned" >&2; exit 2; }
    if [ -n "$out" ]; then
      while IFS= read -r f; do echo "${f#"$dir"/}: $name"; hits=$((hits + 1)); done <<<"$out"
    fi
  done <<<"$PATTERNS"
  # an ssh key or credential file by name
  while IFS= read -r f; do echo "${f#"$dir"/}: credential-file"; hits=$((hits + 1)); done < <(
    find "$dir" \( -path '*/.ssh/id_*' -o -name '.git-credentials' -o -name '.netrc' -o -path '*/.config/gh/hosts.yml' -o -name 'kubeconfig' \) ! -name '*.pub' -type f 2>/dev/null)
  return $((hits > 0))
}

if [ "${1:-}" = "--dir" ] && [ -n "${2:-}" ]; then
  scan_dir "$2" && { echo "scan-secrets: 0 hits"; exit 0; } || { echo "scan-secrets: hits found" >&2; exit 1; }
fi
[ $# -eq 1 ] || { echo "usage: $0 <image> | --dir <dir>" >&2; exit 2; }

work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
docker save "$1" -o "$work/image.tar"
mkdir "$work/img" "$work/files"
tar -xf "$work/image.tar" -C "$work/img"
# image config and manifest (env, history) scanned as text
cp "$work/img"/*.json "$work/files/" 2>/dev/null || true
cp "$work/img"/blobs/sha256/* "$work/files/" 2>/dev/null || true
# every layer, extracted separately so a file deleted in a later layer is still seen
n=0
while IFS= read -r layer; do
  n=$((n + 1)); mkdir "$work/files/layer$n"
  tar -xf "$work/img/$layer" -C "$work/files/layer$n" --exclude='dev/*' 2>/dev/null \
    || { echo "scan-secrets: cannot extract layer $layer: not scanned" >&2; exit 2; }
done < <(python3 -c 'import json,sys; [print(l) for e in json.load(open(sys.argv[1])) for l in e["Layers"]]' "$work/img/manifest.json")
[ "$n" -gt 0 ] || { echo "scan-secrets: no layers found in the image: not scanned" >&2; exit 2; }
# layer files may carry modes that hide them from us; make them readable so none is skipped
chmod -R u+rwX "$work/files" 2>/dev/null || true
if scan_dir "$work/files"; then echo "scan-secrets: 0 hits"; else echo "scan-secrets: hits found" >&2; exit 1; fi
