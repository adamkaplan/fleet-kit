#!/bin/sh
# Bring up the nested Docker daemon when the runtime allows it, install the per-run secrets from files, then run the command.
# Outside Sysbox (no nested support) the worker still starts, without Docker.
#
# The launcher mounts the per-run files read-only under $CW_RUN_DIR (default /run/cw):
#   server-password  the headless server's password, handed to the server through its environment (OpenCode reads it only there)
#   model-auth       the model provider's access entry, as the one-entry array `opencode auth export` prints: IMPORTED
#                    (`opencode auth import --standalone`, read from this file on stdin) into OpenCode's own store under a data
#                    directory on tmpfs, BEFORE the server starts; the server reads only that store. The file is never copied
#   stack-model-key  a stack-only model credential: copied (0600) to $TMP/stack/model-key for cw-stack-up, never exported
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

# #1050: no git call in the pod may open a credential dialog or prompt (the helper answers only for github.com)
GIT_TERMINAL_PROMPT=0; export GIT_TERMINAL_PROMPT
GCM_INTERACTIVE=never; export GCM_INTERACTIVE

if usable server-password; then
  OPENCODE_SERVER_PASSWORD=$(cat "$RUN/server-password"); export OPENCODE_SERVER_PASSWORD
fi

if usable model-auth; then
  # The server reads credentials from its own database under <data dir>/opencode (it ignores an auth.json); the data dir is on tmpfs.
  # The import must succeed before anything starts: a pod that could not take its credential answers nothing (the free fallback model).
  data="$TMP/data"
  ( umask 077; mkdir -p "$data/opencode" )
  XDG_DATA_HOME="$data"; export XDG_DATA_HOME
  if ! opencode auth import --standalone < "$RUN/model-auth" > /dev/null 2>&1; then
    echo "worker: the model credential import failed; refusing to start" >&2
    exit 1
  fi
fi

# #1157: the agent this pod runs, from ONE value with two accepted (unset means worker); anything else refuses the start, before the server
case "${CW_AGENT:-worker}" in
  worker|orchestrator) AGENT="${CW_AGENT:-worker}" ;;
  *) echo "worker: CW_AGENT must be worker or orchestrator; refusing to start" >&2; exit 1 ;;
esac

# the default model and the agent: the pod has no OpenCode config, so write one (no secret in it) on tmpfs and point the server at it
case "${CW_MODEL:-}" in
  *[!A-Za-z0-9._/:-]*) echo "worker: CW_MODEL has a character outside [A-Za-z0-9._/:-]; refusing to start" >&2; exit 1 ;;
esac
cfgdir="$TMP/config"
defn="${CW_SHARE_DIR:-/usr/local/share/cw}/$AGENT.md"
if [ "$AGENT" = orchestrator ]; then
  # a config block cannot beat the definition's own rule (the last match wins): the pod's copy says deny; fail closed, before any copy exists
  if [ "$(grep -c -x -F '  question: allow' "$defn")" != 1 ]; then
    echo "orchestrator: the question permission line was not found exactly once; refusing to start" >&2; exit 1
  fi
fi
( umask 077; mkdir -p "$cfgdir/opencode"
  printf '{"model": "%s",\n' "${CW_MODEL:-github-copilot/claude-sonnet-5.5}" > "$cfgdir/opencode/opencode.json"
  # the worker agent (default) and the shell tool's deny rules: the template's first line is its opening brace
  tail -n +2 "${CW_SHARE_DIR:-/usr/local/share/cw}/opencode-worker.json" >> "$cfgdir/opencode/opencode.json"
  mkdir -p "$cfgdir/opencode/agents"
  if [ "$AGENT" = orchestrator ]; then
    sed 's/^  question: allow$/  question: deny/' "$defn" > "$cfgdir/opencode/agents/$AGENT.md"
  else
    cat "$defn" > "$cfgdir/opencode/agents/$AGENT.md"
  fi )
chmod 600 "$cfgdir/opencode/opencode.json"
XDG_CONFIG_HOME="$cfgdir"; export XDG_CONFIG_HOME

# a hub pod (any agent; only when the launcher wrote the key): the per-pod channel secret goes from the per-run Secret to the memory-backed file the cloud command reads (a copy, 0400, never printed)
if usable channel-secret; then
  install -m 0400 "$RUN/channel-secret" "$TMP/channel-token"
fi

# a stack launched with a stack-only model credential (only when the launcher wrote the key): a 0600 copy on the memory-backed tmpfs for
# cw-stack-up. It is never exported into this environment and never printed; the coder's own model-auth is never copied there.
if usable stack-model-key; then
  ( umask 077; mkdir -p "$TMP/stack" )
  install -m 0600 "$RUN/stack-model-key" "$TMP/stack/model-key"
fi

if usable git-token; then
  bin="$TMP/bin"
  ( umask 077; mkdir -p "$bin" )
  tokfile="$RUN/git-token"   # baked into the generated files: a caller's CW_RUN_DIR changes nothing
  # The helper answers only for https://github.com: the token is never offered to another host. At every call the token
  # file must be readable and not empty, or the call fails with one line (never an empty password, never a value).
  cat > "$bin/git-credential-cw" <<H
#!/bin/sh
[ "\${1:-}" = get ] || exit 0
proto="" host=""
while IFS='=' read -r k v; do
  case "\$k" in protocol) proto=\$v;; host) host=\$v;; "") break;; esac
done
[ "\$proto" = https ] && [ "\$host" = github.com ] || exit 0
tok=\$({ tr -d '\\r\\n' < "$tokfile"; } 2>/dev/null) || tok=""
if [ -z "\$tok" ]; then echo "worker: the git token file is unreadable or empty" >&2; exit 1; fi
printf 'username=x-access-token\\npassword=%s\\n' "\$tok"
H
  chmod 755 "$bin/git-credential-cw"
  # No git-token means no wrappers: the OpenCode deny rules are then the only pod-side layer.
  # The pod-side refusal of merge, force-push, push to the default branch, issue/label writes and settings calls. It is a
  # convenience and NOT the guard: calling the real binary by its path skips it. The guard is the repository's own rules
  # and the token's scope. Installed with the wrappers; a missing guard file refuses the start (no wrapper without it).
  guard="${CW_SHARE_DIR:-/usr/local/share/cw}/guard.sh"
  if [ ! -r "$guard" ] || [ ! -s "$guard" ]; then
    echo "worker: the guard file is unreadable or empty; refusing to start" >&2
    exit 1
  fi
  cat "$guard" > "$bin/cw-guard"; chmod 755 "$bin/cw-guard"
  real_gh="${CW_GH_CLIENT:-/usr/local/bin/cw-gh}"   # the pod's own client: the real gh is not in the image
  # FAIL CLOSED: a pod with a token but without its client refuses to start (no worker holds the token with no wrapper in front of it).
  # CW_GH_CLIENT names the client for tests only; the image never sets it.
  if [ ! -x "$real_gh" ]; then
    echo "worker: the gh client $real_gh is missing or not executable; refusing to start" >&2
    exit 1
  fi
  real_git=$(command -v git || true)
  if [ -n "$real_gh" ]; then
    cat > "$bin/gh" <<H
#!/bin/sh
# refuse the six (exit 3, one line), then run the pod's GitHub client (cw-gh). The token is read for this one call and lives only in the environment of that process
"$bin/cw-guard" gh "\$@" || exit \$?
tok=\$({ tr -d '\\r\\n' < "$tokfile"; } 2>/dev/null) || tok=""
if [ -z "\$tok" ]; then echo "worker: the git token file is unreadable or empty" >&2; exit 1; fi
GH_TOKEN=\$tok exec "$real_gh" "\$@"
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

# #1200 (S14): the record of the pull requests this pod creates through its client, one number a line. The directory is private to this
# user (0700, inside the worker-private $TMP); the client creates the file (0600, never through a symlink) when it records a number, so
# a pod with no pull request of its own has no file and the guard refuses an edit (pr-edit-no-record). The agent and the client are the
# same user: the record is not a security boundary (see docs/cloud-worker-interface.md); the shell guard row only refuses a command that
# names it. Exported before the exec so the agent's command and its shell tool inherit it.
( umask 077; mkdir -p "$TMP/pr-record" )
chmod 700 "$TMP/pr-record"
export CW_PR_RECORD="$TMP/pr-record/numbers"

exec "$@"
