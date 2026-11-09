#!/usr/bin/env bash
# THE publish step for the worker image: scan the staged image layer by layer, then publish BY DIGEST, or publish nothing.
#   publish-image.sh --tool crane|skopeo [--tool-dir <dir>] --staging <registry/repo:tag> --release <registry/repo:tag>
# 1. resolve the staging tag to a digest D   2. export exactly <staging>@D   3. scan-secrets.sh --archive on the export
# 4. resolve the staging tag AGAIN: if it is no longer D, refuse   5. copy <staging>@D to the release tag (by digest, so a
# tag that moved cannot publish an unscanned image).
# Exit 0 = published; 1 = the scan found a hit; 2 = a layer or archive could not be read (not scanned); 3 = a tool failed,
# a digest could not be resolved, the staging tag moved, or the arguments are wrong. In every non-zero case NOTHING is
# published. A registry login must already exist: this script never reads or prints a credential. The tool is chosen by
# argument from a fixed list and run from an absolute path (from --tool-dir, else a fixed list of system directories): the
# environment does not choose it. The staging digest must be a single-image (linux/amd64) manifest: an index or manifest list is refused (exit 3) before anything is exported.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
die() { echo "publish-image: $1" >&2; exit "${2:-3}"; }

tool="" tooldir="" staging="" release=""
while [ $# -gt 0 ]; do
  [ $# -ge 2 ] || die "missing value for $1"
  case $1 in
    --tool) tool=$2 ;; --tool-dir) tooldir=$2 ;; --staging) staging=$2 ;; --release) release=$2 ;;
    *) die "unknown argument $1" ;;
  esac
  shift 2
done
case $tool in crane|skopeo) ;; *) die "--tool must be crane or skopeo" ;; esac
REF='^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+:[A-Za-z0-9._-]+$'
[[ $staging =~ $REF ]] || die "--staging must be <registry>/<repo>:<tag> (no digest)"
[[ $release =~ $REF ]] || die "--release must be <registry>/<repo>:<tag> (no digest)"
[ "$staging" != "$release" ] || die "--staging and --release must differ"

bin=""
if [ -n "$tooldir" ]; then
  [[ $tooldir = /* ]] || die "--tool-dir must be an absolute path"
  [ -x "$tooldir/$tool" ] && bin="$tooldir/$tool"
else
  for d in /usr/local/bin /opt/homebrew/bin /usr/bin /bin; do [ -x "$d/$tool" ] && { bin="$d/$tool"; break; }; done
fi
[ -n "$bin" ] || die "$tool not found in a fixed location (use --tool-dir)"

work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
sha() { python3 -c 'import hashlib,sys; print("sha256:"+hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'; }
digest_of() { # the digest of the staging tag NOW
  local d
  case $tool in
    crane) d=$("$bin" digest "$staging") || return 1 ;;
    skopeo) d=$("$bin" inspect --raw "docker://$staging" | sha) || return 1 ;;
  esac
  [[ $d =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
  printf '%s' "$d"
}

D=$(digest_of) || die "cannot resolve the digest of $staging: nothing published"
src=${staging%:*}@$D
# ONLY a single-image manifest is published: an index or manifest list would carry platforms this script does not scan
case $tool in
  crane) m=$("$bin" manifest "$src") || die "cannot read the manifest of $src: nothing published" ;;
  skopeo) m=$("$bin" inspect --raw "docker://$src") || die "cannot read the manifest of $src: nothing published" ;;
esac
printf '%s' "$m" | python3 -c '
import json, sys
d = json.load(sys.stdin)
ok = isinstance(d, dict) and "manifests" not in d and isinstance(d.get("layers"), list) and d.get("mediaType", "application/vnd.oci.image.manifest.v1+json") in (
    "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json")
sys.exit(0 if ok else 1)' 2>/dev/null || die "$src is not a single-image manifest (an index or manifest list?): refused, nothing published"
case $tool in
  crane)
    "$bin" pull --platform linux/amd64 --format oci "$src" "$work/layout" >/dev/null || die "cannot export $src: nothing published"
    tar -C "$work/layout" -cf "$work/image.tar" . || die "cannot pack the export of $src: nothing published" ;;
  skopeo)
    "$bin" copy --override-arch amd64 "docker://$src" "oci-archive:$work/image.tar" >/dev/null || die "cannot export $src: nothing published" ;;
esac

rc=0
env -u SCAN_ALLOWLIST -u SCAN_TRIPLES "$HERE/scan-secrets.sh" --archive "$work/image.tar" || rc=$?
[ "$rc" -eq 0 ] || { echo "publish-image: scan of $src exit $rc: nothing published" >&2; exit "$rc"; }

D2=$(digest_of) || die "cannot re-resolve the digest of $staging: nothing published"
[ "$D2" = "$D" ] || die "$staging moved from $D to $D2 after the scan: nothing published"

case $tool in
  crane) "$bin" copy "$src" "$release" || die "cannot publish $release: tool failed" ;;
  skopeo) "$bin" copy "docker://$src" "docker://$release" || die "cannot publish $release: tool failed" ;;
esac
echo "publish-image: published $release from $src (scanned digest $D)"
