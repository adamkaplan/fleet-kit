#!/bin/sh
# Bring up the nested Docker daemon when the runtime allows it, install the per-run secrets from files, then run the command.
# Outside Sysbox (no nested support) the worker still starts, without Docker.
#
# The launcher mounts the per-run files read-only under $CW_RUN_DIR (default /run/cw):
#   server-password  the headless server's password, handed to the server through its environment (OpenCode reads it only there)
#   model-auth       the model provider's access entry (JSON): COPIED, mode 600, into a data directory on tmpfs, so the
#                    server can refresh its short-lived token in the copy and never write it back to the Secret
#   git-token        read at use time by a git credential helper and a gh wrapper; never in git config, argv or the environment
# A file that is absent is skipped (a pod started without per-run secrets). A file that is present but unreadable or empty
# REFUSES the start: there is no fallback to another source. Nothing here prints a value.
set -eu
RUN="${CW_RUN_DIR:-/run/cw}"
TMP="${CW_TMP_DIR:-/run/cw-tmp}"   # tmpfs (an in-memory emptyDir), writable by the worker user

# 0 when the file is present and usable, 1 when absent; refuses (exit 1) when present but unreadable or empty
usable() {
  [ -e "$RUN/$1" ] || return 1
  if [ ! -r "$RUN/$1" ] || [ ! -s "$RUN/$1" ]; then
    echo "worker: the per-run file $1 is unreadable or empty; refusing to start" >&2
    exit 1
  fi
}

if [ "${WORKER_DOCKER:-1}" = "1" ]; then
  sudo /usr/local/bin/start-dockerd || echo "worker: nested Docker unavailable (see /var/log/dockerd.log)" >&2
fi

if usable server-password; then
  OPENCODE_SERVER_PASSWORD=$(cat "$RUN/server-password"); export OPENCODE_SERVER_PASSWORD
fi

if usable model-auth; then
  # OpenCode keeps provider credentials in <data dir>/opencode/auth.json; the data dir is moved onto tmpfs
  data="$TMP/data"
  ( umask 077; mkdir -p "$data/opencode"; cat "$RUN/model-auth" > "$data/opencode/auth.json" )
  chmod 600 "$data/opencode/auth.json"
  XDG_DATA_HOME="$data"; export XDG_DATA_HOME
fi

# the default model and the worker agent: the pod has no OpenCode config, so write one (no secret in it) on tmpfs and point the server at it
cfgdir="$TMP/config"
( umask 077; mkdir -p "$cfgdir/opencode"
  printf '{"model": "%s",\n' "${CW_MODEL:-github-copilot/claude-sonnet-5.5}" > "$cfgdir/opencode/opencode.json"
  # the worker agent (default) and the shell tool's deny rules: the template's first line is its opening brace
  tail -n +2 "${CW_SHARE_DIR:-/usr/local/share/cw}/opencode-worker.json" >> "$cfgdir/opencode/opencode.json"
  mkdir -p "$cfgdir/opencode/agents"
  cat "${CW_SHARE_DIR:-/usr/local/share/cw}/worker.md" > "$cfgdir/opencode/agents/worker.md" )
chmod 600 "$cfgdir/opencode/opencode.json"
XDG_CONFIG_HOME="$cfgdir"; export XDG_CONFIG_HOME

if usable git-token; then
  bin="$TMP/bin"
  ( umask 077; mkdir -p "$bin" )
  # The helper answers only for https://github.com: the token is never offered to another host.
  cat > "$bin/git-credential-cw" <<'H'
#!/bin/sh
[ "${1:-}" = get ] || exit 0
proto="" host=""
while IFS='=' read -r k v; do
  case "$k" in protocol) proto=$v;; host) host=$v;; "") break;; esac
done
[ "$proto" = https ] && [ "$host" = github.com ] || exit 0
printf 'username=x-access-token\npassword='
tr -d '\r\n' < "${CW_RUN_DIR:-/run/cw}/git-token"
printf '\n'
H
  chmod 755 "$bin/git-credential-cw"
  # The pod-side refusal of merge, force-push, push to the default branch, issue/label writes and settings calls. It is a
  # convenience and NOT the guard: calling the real binary by its path skips it. The guard is the repository's own rules
  # and the token's scope. Installed with the wrappers; a missing guard file refuses the start (no wrapper without it).
  guard="${CW_SHARE_DIR:-/usr/local/share/cw}/guard.sh"
  if [ ! -r "$guard" ] || [ ! -s "$guard" ]; then
    echo "worker: the guard file is unreadable or empty; refusing to start" >&2
    exit 1
  fi
  cat "$guard" > "$bin/cw-guard"; chmod 755 "$bin/cw-guard"
  real_gh=$(command -v gh || true)
  real_git=$(command -v git || true)
  if [ -n "$real_gh" ]; then
    cat > "$bin/gh" <<H
#!/bin/sh
# refuse the six (exit 3, one line), then run gh. The token is read for this one call and lives only in the environment of that gh process
"$bin/cw-guard" gh "\$@" || exit \$?
GH_TOKEN=\$(tr -d '\r\n' < "\${CW_RUN_DIR:-/run/cw}/git-token") exec "$real_gh" "\$@"
H
    chmod 755 "$bin/gh"
  fi
  if [ -n "$real_git" ]; then
    cat > "$bin/git" <<H
#!/bin/sh
CW_REAL_GIT="$real_git" "$bin/cw-guard" git "\$@" || exit \$?
exec "$real_git" "\$@"
H
    chmod 755 "$bin/git"
  fi
  # ~/.gitconfig is read by every process of this user, including a fresh-environment `kubectl exec` (the end hook)
  git config --global credential.helper "$bin/git-credential-cw"
  PATH="$bin:$PATH"; export PATH
fi

exec "$@"
