#!/bin/sh
# A stand-in `opencode auth export` for the launcher tests. The launcher scrubs the environment, so the stand-in reads HOME only:
# it records its arguments and its environment, prints $HOME/export.json, and exits with the code in $HOME/export.exit (default 0).
echo "$*" > "$HOME/oc-argv"
env > "$HOME/oc-env"
# when $HOME/export.stdin exists it records what the stdin held (empty when the launcher gave it /dev/null)
[ -e "$HOME/export.stdin" ] && cat > "$HOME/oc-stdin"
[ -e "$HOME/export.json" ] && cat "$HOME/export.json"
[ -e "$HOME/export.exit" ] && exit "$(cat "$HOME/export.exit")"
exit 0
