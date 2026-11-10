#!/bin/sh
# cw-guard <gh|git> <args...>: the pod's convenience refusal of six kinds of command (plus the few extras listed in cloud/README.md).
#   exit 0  the command is none of the six: the caller runs it unchanged
#   exit 3  refused: ONE line on stderr, "refused: <what>: ...". <what> is a fixed phrase; no argument value is ever printed.
#
# HONESTY: this is defence in depth and a convenience, NOT the guard. It runs only when the caller goes through the `gh` and
# `git` wrappers first on PATH; calling the real binary by its path, another HTTP client, or a script skips it. The guard is the
# repository's own rules (branch protection or a ruleset: no merge, no push to the default branch, no force-push) and the
# token's scope (preflight, #545). Do not rely on this file for either.
#
# Fail mode: an internal failure of the guard itself stops the command (the wrappers run `guard || exit $?`); an option the parser cannot
# read is let through (fail open), and the default-branch set degrades to main and master when origin/HEAD is unreadable
# (so `git push origin trunk` then passes when the default is `trunk` and CW_DEFAULT_BRANCH is not set). Over-refusals: a
# `gh` first word it does not know is refused (it could be an alias); a refspec with `*` is refused. A pod
# started without a git-token gets no wrappers at all: then the OpenCode deny rules are the only pod-side layer.
# Env: CW_REAL_GIT (the real git, used to read the current branch), CW_DEFAULT_BRANCH (extra default branch names,
# space or comma separated). `main` and `master` and the branch origin/HEAD points to are always treated as default.
# THE git config TABLE (the one place the forms are listed): `#CFG <refused|allowed> | <name> | <example args> | <why>`. The test
# runs every row through the wrapper, and checks that the README paragraph names exactly these names. A row is a FAMILY member:
# add the form here, in the code below and in cloud/README.md together.
#CFG refused | --rename-section | --rename-section old new | writes configuration
#CFG refused | --remove-section | --remove-section old | writes configuration
#CFG refused | -e | -e | opens the file to edit
#CFG refused | --edit | --edit | opens the file to edit
#CFG refused | --add | --add user.x y | writes configuration
#CFG refused | --unset | --unset user.x | writes configuration
#CFG refused | --unset-all | --unset-all user.x | writes configuration
#CFG refused | --replace-all | --replace-all user.x y | writes configuration
#CFG refused | --file | --file=/tmp/f user.x | a write to (or read of) another file
#CFG refused | -f | -f /tmp/f user.x | a write to (or read of) another file
#CFG refused | --blob | --blob=x user.x | another source of configuration
#CFG refused | an unknown flag or abbreviation | --uns user.x | git accepts a unique prefix of --unset; any flag not listed as a read or display flag is refused
#CFG refused | edit | edit | the subcommand form (git 2.46 and later)
#CFG refused | set | set user.x y | the subcommand form
#CFG refused | unset | unset user.x | the subcommand form
#CFG refused | rename-section | rename-section a b | the subcommand form
#CFG refused | remove-section | remove-section a | the subcommand form
#CFG refused | a value that looks like an option | core.pager --default=x;touch/tmp/p | everything after the key is a value; a word after the key starting with - is refused, never trusted
#CFG refused | a value word after a read mode's key | --get-all core.x v | refused although git takes it as a read (--get K REGEX, --get-all K REGEX, --get-regexp P VALREGEX): the key count must match the mode
#CFG refused | an option after the get or list subcommand | list --show-origin | refused although git takes it as a read (config list --show-origin, config get --all K): options come before the key
#CFG refused | KEY VALUE | user.x y | the plain write form (any key but the two identity keys below)
#CFG refused | include.path | include.path /tmp/f | a value that makes git read another file
#CFG refused | includeIf.<cond>.path | includeIf.gitdir:/a/.path /tmp/f | a value that makes git read another file
#CFG allowed | user.name | user.name x | identity, needed to commit (exactly this key; user.name.x is not it)
#CFG allowed | user.email | user.email x@y | identity, needed to commit (exactly this key)
#CFG allowed | --get | --get user.x | a read, at most one key
#CFG allowed | --get-all | --get-all user.x | a read
#CFG allowed | --get-regexp | --get-regexp user | a read
#CFG allowed | --list | --list | a read
#CFG allowed | --list --show-origin | --list --show-origin | a read
# THE SCOPE (ask #2354, #2360): in_tmp_scope below is true only when the command runs in a repository whose physical git directory (bare or not) is under /tmp (physical paths, links
# resolved; the -C directory counts; TMPDIR must itself resolve under /tmp; never /work/repo or a path that contains it or lies in it).
# Only in scope: (a) a push (also forced: --force, --force-with-lease, a + refspec; never --mirror, --all, a glob) whose remote is a local path or file:// URL under /tmp, (b) `git config KEY VALUE` for a
# key not in the table below, (c) GIT_CONFIG_GLOBAL and GIT_CONFIG_SYSTEM naming a plain file under /tmp or the checkout server/tests (the #ENVCFG rows). Out of scope: as before.
# THE EXCLUDED KEYS (in scope `git config KEY VALUE` is allowed for every key but these; keys compared in lower case; the README names them):
#CFGX excluded | alias.* | a command that runs a program
#CFGX excluded | push.default | changes what a push sends
#CFGX excluded | core.hooksPath | runs hooks from another directory
#CFGX excluded | core.sshCommand | runs a program
#CFGX excluded | core.fsmonitor | runs a program
#CFGX excluded | core.pager | runs a program
#CFGX excluded | core.editor | runs a program
#CFGX excluded | core.askpass | runs a program
#CFGX excluded | core.gitProxy | runs a program
#CFGX excluded | credential.* | a credential helper runs a program
#CFGX excluded | include.* | makes git read another file
#CFGX excluded | includeIf.* | makes git read another file
#CFGX excluded | url.* | rewrites a remote
#CFGX excluded | http.proxy* | reaches the network by a proxy
#CFGX excluded | http.sslcainfo | trusts another certificate file
#CFGX excluded | http.cookiefile | reads another file
#CFGX excluded | remote.*.proxy | reaches the network by a proxy
#CFGX excluded | remote.*.receivepack | runs a program
#CFGX excluded | remote.*.uploadpack | runs a program
#CFGX excluded | uploadpack.packObjectsHook | runs a program
#CFGX excluded | diff.external | runs a program
#CFGX excluded | filter.* | runs a program
#CFGX excluded | merge.*.driver | runs a program
#CFGX excluded | gpg.*.program | runs a program
#CFGX excluded | sendemail.* | mail settings
#CFGX excluded | protocol.ext.allow | allows the ext transport (a program)
#CFGX excluded | gpg.program | runs a program
#CFGX excluded | core.worktree | points the repository at another work tree
#CFGX excluded | core.alternateRefsCommand | runs a program
#CFGX excluded | sequence.editor | runs a program
#CFGX excluded | pager.* | runs a program
#CFGX excluded | remote.*.vcs | runs a program
# THE git ENVIRONMENT TABLE (the same rule for the variables of the family GIT_CONFIG_*): `#ENVCFG <refused|allowed> | <name> | <form> | <why>`.
#   Only these pass, an exact allow-list (never a prefix); every other GIT_CONFIG_* set and not empty is refused with the one line.
#ENVCFG allowed | GIT_CONFIG_NOSYSTEM | 1 | only the value 1
#ENVCFG allowed | GIT_CONFIG_SYSTEM | /dev/null, or in scope a regular file under /tmp or /work/repo/server/tests | /dev/null always; a file only in scope (in_tmp_scope: the git directory of the repository the command runs in, bare or not, is under /tmp, never /work/repo), a regular file (not a link), at most 64 KiB, owned by the running user, physical path under /tmp or under the checkout server/tests
#ENVCFG allowed | GIT_CONFIG_GLOBAL | /dev/null, or in scope a regular file under /tmp or /work/repo/server/tests | /dev/null always; a file only in scope (in_tmp_scope: the git directory of the repository the command runs in, bare or not, is under /tmp, never /work/repo), a regular file (not a link), at most 64 KiB, owned by the running user, physical path under /tmp or under the checkout server/tests
#ENVCFG allowed | GIT_CONFIG_COUNT | 1 to 16 | a whole number; KEY_n and VALUE_n for n below it; a KEY_n beyond it is refused
#ENVCFG allowed | user.name | any printable value on one line | identity (the key is compared in lower case)
#ENVCFG allowed | user.email | any printable value on one line | identity
#ENVCFG allowed | core.excludesFile | /etc/gitignore | only this value
#ENVCFG allowed | maintenance.auto | false | only this value
#ENVCFG allowed | gc.auto | 0 | only this value
#ENVCFG allowed | credential.helper | an empty value | only empty (it clears a helper)
#ENVCFG allowed | url.<base>.insteadOf | https://github.com/ | only for a base that is an absolute local path or a file:// URL of one (no .. segment), and only this value
#ENVCFG allowed | http.extraHeader | Authorization: Basic <base64> | clause 7 (ask #2360): only in scope, ONE line starting `Authorization: Basic ` and a base64 word; a second line, another header or out of scope is refused
#ENVCFG allowed | http.<url>.extraHeader | X-Test: 1 | only scoped to http(s)://127.0.0.1 or localhost (port and path optional, no user information)
#ENVCFG refused | GIT_CONFIG_PARAMETERS | x | carries any setting
#ENVCFG refused | GIT_CONFIG_GLOBAL or GIT_CONFIG_SYSTEM of any other value | /tmp/x | out of scope any value but /dev/null; in scope a link, a directory, a file over 64 KiB, a path with .. or . segments, a file of another owner or outside /tmp and server/tests
#ENVCFG refused | GIT_CONFIG_COUNT outside 1 to 16 | 17 | not a whole number from 1 to 16
#ENVCFG refused | a key outside the list | core.hooksPath | runs a program (also core.sshCommand, core.fsmonitor, core.pager, include.*, alias.*, push.default, any other credential.*, any other http.* and http.extraHeader out of scope or with a value other than one Authorization: Basic line)
#ENVCFG refused | credential.helper with a value | store | a helper runs a program
#ENVCFG refused | a missing or empty KEY_n, or a KEY_n beyond the count | GIT_CONFIG_KEY_3 | git ignores or misreads it
tool=${1:-}; [ $# -gt 0 ] && shift
tail_msg="the repository's rules are the guard; this wrapper is only a convenience"
refuse() { echo "refused: $1: $tail_msg" >&2; exit 3; }
real_git="${CW_REAL_GIT:-git}"

defaults() {
  printf '%s\n' main master
  for b in $(printf '%s' "${CW_DEFAULT_BRANCH:-}" | tr ',' ' '); do printf '%s\n' "$b"; done
  "$real_git" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||'
}
is_default() {   # $1: a branch name, refs/heads/ stripped
  b=${1#refs/heads/}
  defaults | grep -qxF -- "$b"
}

bad_cfg() {   # $1: a git config name or name=value: an alias or a push.default override
  k=$(printf '%s' "$1" | tr 'A-Z' 'a-z')
  case "$k" in alias.*) refuse "defining a git alias" ;; push.default*) refuse "overriding push.default" ;; esac
}

env_cfg_refuse() { refuse "git configuration from the environment (GIT_CONFIG_*)"; }
env_cfg_text() {   # $1: a value: printable characters, one line, not empty
  case "$1" in "") env_cfg_refuse ;; *"
"*) env_cfg_refuse ;; esac
  printf '%s' "$1" | LC_ALL=C grep -q '[^ -~]' && env_cfg_refuse
  return 0
}
env_cfg_basic() {   # $1: a header value: ONE line, `Authorization: Basic ` and a base64 word
  env_cfg_text "$1"
  printf '%s' "$1" | grep -Eq '^Authorization: Basic [A-Za-z0-9+/=]+$' || env_cfg_refuse
  return 0
}
env_cfg_local() {   # $1: an absolute local path (letters, digits, dot, underscore, dash, slash; no .. segment) or a file:// URL of one
  p=${1#file://}
  printf '%s' "$p" | grep -Eq '^/[A-Za-z0-9._/-]*$' || env_cfg_refuse
  case "$p/" in */../*) env_cfg_refuse ;; esac
  return 0
}
env_cfg_pair() {   # $1: a key, $2: its value: only the listed settings pass (the key is compared in lower case; a URL part keeps its case)
  case "$1" in *"
"*) env_cfg_refuse ;; esac   # grep judges one line at a time: a key of two lines is refused
  lk=$(printf '%s' "$1" | tr 'A-Z' 'a-z')
  case "$lk" in
    user.email|user.name) env_cfg_text "$2" ;;
    core.excludesfile) [ "$2" = /etc/gitignore ] || env_cfg_refuse ;;
    maintenance.auto) [ "$2" = false ] || env_cfg_refuse ;;
    gc.auto) [ "$2" = 0 ] || env_cfg_refuse ;;
    credential.helper) [ -z "$2" ] || env_cfg_refuse ;;
    url.?*.insteadof)
      b=${1#????}; b=${b%??????????}
      env_cfg_local "$b"; [ "$2" = https://github.com/ ] || env_cfg_refuse ;;
    http.extraheader) in_tmp_scope || env_cfg_refuse; env_cfg_basic "$2" ;;   # ask #2360 clause 7: in scope only, one Authorization: Basic line
    http.?*.extraheader)
      b=${1#?????}; b=${b%????????????}
      printf '%s' "$b" | grep -Eq '^https?://(127\.0\.0\.1|localhost)(:[0-9]+)?(/[A-Za-z0-9._~%/-]*)?$' || env_cfg_refuse
      env_cfg_text "$2" ;;
    *) env_cfg_refuse ;;
  esac
}
# env_cfg: see the #ENVCFG rows; only the listed settings pass, any other GIT_CONFIG_* set and not empty is refused
env_cfg() {   # git's environment configuration is the environment form of the two -c refusals: any of it set and not empty is refused
  [ -n "${GIT_CONFIG_PARAMETERS:-}" ] && env_cfg_refuse
  case "${GIT_CONFIG_NOSYSTEM:-}" in ""|1) ;; *) env_cfg_refuse ;; esac
  case "${GIT_CONFIG_GLOBAL:-}" in ""|/dev/null) ;; *) scope_file "$GIT_CONFIG_GLOBAL" ;; esac
  case "${GIT_CONFIG_SYSTEM:-}" in ""|/dev/null) ;; *) scope_file "$GIT_CONFIG_SYSTEM" ;; esac
  cnt=${GIT_CONFIG_COUNT:-}
  case "$cnt" in ""|[1-9]|1[0-6]) ;; *) env_cfg_refuse ;; esac
  keys=$(env | grep '^GIT_CONFIG_KEY_[A-Za-z0-9_]*=.')
  if [ -n "$keys" ]; then
    [ -n "$cnt" ] || env_cfg_refuse
    while IFS= read -r kl; do
      kn=${kl%%=*}; kn=${kn#GIT_CONFIG_KEY_}
      case "$kn" in [1-9]|1[0-5]|0) [ "$kn" -lt "$cnt" ] || env_cfg_refuse ;; *) env_cfg_refuse ;; esac
    done <<EOF
$keys
EOF
  fi
  i=0
  while [ -n "$cnt" ] && [ "$i" -lt "$cnt" ]; do
    eval "ek=\${GIT_CONFIG_KEY_$i:-}; ev=\${GIT_CONFIG_VALUE_$i:-}"
    [ -n "$ek" ] || env_cfg_refuse
    env_cfg_pair "$ek" "$ev"
    i=$((i + 1))
  done
  return 0
}

# ---- the scope (ask #2354) ----
sc_dir=.; sc_off=0
sgit() {   # the real git for the scope's own reads: no configuration of the environment is trusted before the scope is known
  env -u GIT_CONFIG_COUNT -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG_KEY_0 -u GIT_CONFIG_VALUE_0 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_NOSYSTEM=1 "$real_git" "$@"
}
scope_args() {   # the words after `git`: remember the last -C directory (chained), and whether the repository is chosen another way
  sc_dir=.; sc_off=0
  while [ $# -gt 0 ]; do
    case "$1" in
      -C) [ $# -ge 2 ] || break; case "$2" in /*) sc_dir=$2 ;; *) sc_dir=$sc_dir/$2 ;; esac; shift 2 ;;
      -c|--config-env) sc_off=1; shift 2 || break ;;   # ask #2360 (security review): a -c setting can move a remote, so the command is out of scope
      --config-env=*) sc_off=1; shift ;;
      --namespace|--exec-path) shift 2 || break ;;
      --git-dir|--work-tree) sc_off=1; shift 2 || break ;;
      --git-dir=*|--work-tree=*) sc_off=1; shift ;;
      -*) shift ;;
      *) break ;;
    esac
  done
}
phys() {   # $1: a directory: its physical path (links resolved), or fails
  [ -n "$1" ] || return 1
  ( cd -P -- "$1" 2>/dev/null && pwd -P )
}
tmp_under() {   # $1: a physical path: true when strictly under the physical /tmp and unrelated to the work checkout /work/repo
  tu_t=$(phys /tmp) || return 1
  tu_w=$(phys /work/repo) || tu_w=/work/repo
  case "$1" in *"
"*) return 1 ;; esac
  case "$1" in "$tu_t"/?*) ;; *) return 1 ;; esac
  case "$1" in "$tu_w"|"$tu_w"/*|/work/repo|/work/repo/*) return 1 ;; esac
  case "$tu_w/" in "$1"/*) return 1 ;; esac
  case "/work/repo/" in "$1"/*) return 1 ;; esac
  return 0
}
in_tmp_scope() {   # true only for a command that runs in a throwaway repository under /tmp (see the SCOPE note at the top)
  [ "$sc_off" = 0 ] || return 1
  [ -z "${GIT_DIR:-}${GIT_WORK_TREE:-}${GIT_COMMON_DIR:-}${GIT_NAMESPACE:-}" ] || return 1
  is_t=$(phys /tmp) || return 1
  if [ -n "${TMPDIR:-}" ]; then   # TMPDIR must itself resolve under /tmp
    is_td=$(phys "$TMPDIR") || return 1
    case "$is_td" in "$is_t"|"$is_t"/*) ;; *) return 1 ;; esac
  fi
  is_gd=$(cd -P -- "$sc_dir" 2>/dev/null && sgit rev-parse --absolute-git-dir 2>/dev/null) || return 1   # ask #2360: the physical git directory decides, a bare repository has no work tree
  is_gd=$(phys "$is_gd") || return 1
  tmp_under "$is_gd" || return 1
  is_top=$(cd -P -- "$sc_dir" 2>/dev/null && sgit rev-parse --show-toplevel 2>/dev/null) || is_top=""
  if [ -n "$is_top" ]; then is_top=$(phys "$is_top") || return 1; tmp_under "$is_top" || return 1; fi   # a work tree, when there is one, must be under /tmp too
  is_cd=$(cd -P -- "$sc_dir" 2>/dev/null && sgit rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || return 1
  is_cd=$(phys "$is_cd") || return 1   # a worktree or a .git file that shares the work checkout's repository is out of scope
  tmp_under "$is_cd" || return 1
  return 0
}
scope_file() {   # $1: a path named by GIT_CONFIG_GLOBAL or GIT_CONFIG_SYSTEM: refuse unless in scope and a plain file under /tmp
  in_tmp_scope || env_cfg_refuse
  case "$1" in /*) ;; *) env_cfg_refuse ;; esac
  case "$1/" in */../*|*/./*|*//*) env_cfg_refuse ;; esac
  [ -f "$1" ] && [ ! -L "$1" ] && [ -O "$1" ] || env_cfg_refuse
  sf_n=$(wc -c < "$1" 2>/dev/null) || env_cfg_refuse
  [ "${sf_n:-65537}" -le 65536 ] 2>/dev/null || env_cfg_refuse
  sf_d=$(phys "${1%/*}/") || env_cfg_refuse
  tmp_under "$sf_d/${1##*/}" && return 0
  sf_t=$(phys /work/repo/server/tests) || env_cfg_refuse   # ask #2360: in scope, a fixture file under the checkout's server/tests (physical path)
  case "$sf_d/${1##*/}" in "$sf_t"/?*) return 0 ;; esac
  env_cfg_refuse
}
cfg_scoped_ok() {   # in scope: `git config [--local] KEY VALUE` for a key not in the #CFGX table
  [ "${1:-}" = --local ] && shift
  [ $# -eq 2 ] || return 1
  case "$1" in -*) return 1 ;; esac
  case "$2" in -*) return 1 ;; esac
  ck=$(printf '%s' "$1" | tr 'A-Z' 'a-z')
  case "$ck" in *.*) ;; *) return 1 ;; esac
  case "$ck" in http.extraheader|http.*.extraheader) case "$2" in *"
"*|*""*) return 1 ;; esac; printf '%s' "$2" | grep -Eq '^Authorization: Basic [A-Za-z0-9+/=]+$' || return 1 ;; esac   # ask #2360 clause 7
  case "$ck" in
    alias.*|push.default|core.hookspath|core.sshcommand|core.fsmonitor|core.pager|core.editor|core.askpass|core.gitproxy) return 1 ;;
    credential.*|include.*|includeif.*|url.*|http.proxy*|http.sslcainfo|http.cookiefile|http.*.proxy*|http.*.sslcainfo|http.*.cookiefile) return 1 ;;
    remote.*.proxy*|remote.*.receivepack|remote.*.uploadpack|uploadpack.packobjectshook|diff.external|filter.*|merge.*.driver|gpg.*.program|sendemail.*|protocol.ext.allow|gpg.program|core.worktree|core.alternaterefscommand|sequence.editor|pager.*|remote.*.vcs) return 1 ;;
  esac
  return 0
}
push_remote() {   # the words after `push`: the remote word, as the loop below counts it (empty when none)
  pr_n=0; pr_skip=0; pr_r=""
  for a in "$@"; do
    if [ "$pr_skip" = 2 ]; then pr_r=$a; pr_skip=0; continue; fi
    if [ "$pr_skip" = 1 ]; then pr_skip=0; continue; fi
    case "$a" in
      --repo) pr_skip=2; pr_n=$((pr_n + 1)); continue ;;
      --repo=*) pr_r=${a#--repo=}; pr_n=$((pr_n + 1)); continue ;;
      -o|--push-option|--receive-pack|--exec) pr_skip=1; continue ;;
      -[!-]*o) pr_skip=1; continue ;;
      --push-option=*|--receive-pack=*|--exec=*) continue ;;
      -*) continue ;;
    esac
    pr_n=$((pr_n + 1))
    [ "$pr_n" -eq 1 ] && [ -z "$pr_r" ] && pr_r=$a
  done
  printf '%s' "$pr_r"
}
push_scope_check() {   # the words after `push`: true in scope when the remote is a local path or file:// URL under /tmp
  in_tmp_scope || return 1
  ps_k=0
  for a in "$@"; do   # fail closed: git takes a unique prefix of a long option, attaches a value to a short one and lets a positional repository beat --repo, so only exact known flags are read
    if [ "$ps_k" = 1 ]; then ps_k=0; continue; fi   # the value word of -o, --push-option, --receive-pack, --exec
    case "$a" in
      -o|--push-option|--receive-pack|--exec) ps_k=1 ;;
      --force|--force-with-lease|--force-with-lease=*|--force-if-includes) ;;   # ask #2360: forced, allowed only when the whole push is in scope
      --dry-run|--verbose|--quiet|--set-upstream|--tags|--follow-tags|--no-verify|--progress|--atomic|--porcelain|--prune|--delete|--thin|--no-thin) ;;
      --push-option=*|--receive-pack=*|--exec=*) ;;
      -|-*[!unvqdf46-]*|--*) return 1 ;;
    esac
  done
  ps_r=$(push_remote "$@")
  if [ -z "$ps_r" ]; then
    ps_c=$(sgit -C "$sc_dir" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    ps_r=""
    [ -n "$ps_c" ] && ps_r=$(sgit -C "$sc_dir" config --get "branch.$ps_c.pushremote" 2>/dev/null || true)
    [ -n "$ps_r" ] || ps_r=$(sgit -C "$sc_dir" config --get remote.pushdefault 2>/dev/null || true)
    [ -n "$ps_c" ] && [ -z "$ps_r" ] && ps_r=$(sgit -C "$sc_dir" config --get "branch.$ps_c.remote" 2>/dev/null || true)
    [ -n "$ps_r" ] || ps_r=origin
  fi
  case "$ps_r" in
    /*|file://*)   # a path or URL word: git still uses a configured remote of that name, and any rewrite rule of a configuration file
      case "${GIT_CONFIG_GLOBAL:-/dev/null}${GIT_CONFIG_SYSTEM:-/dev/null}" in /dev/null/dev/null) ;; *) return 1 ;; esac
      [ -z "$("$real_git" -C "$sc_dir" config --get "remote.$ps_r.url" 2>/dev/null)$("$real_git" -C "$sc_dir" config --get "remote.$ps_r.pushurl" 2>/dev/null)" ] || return 1
      [ -z "$("$real_git" -C "$sc_dir" config --get-regexp '^url\..*\.(push)?insteadof$' 2>/dev/null)" ] || return 1   # the real configuration: $HOME, XDG and /etc files too
      ps_u=$ps_r ;;
    -*|*:*|*..*|*/*) return 1 ;;
    *) ps_u=$("$real_git" -C "$sc_dir" remote get-url --push --all "$ps_r" 2>/dev/null) || return 1 ;;   # the real configuration (env_cfg has judged it): rewrites apply
  esac
  while IFS= read -r ps_one; do   # every push URL of the remote must be local under /tmp (git pushes to all of them)
    case "$ps_one" in *%*) return 1 ;; file:///*) ps_one=${ps_one#file://} ;; file:*) return 1 ;; /*) ;; *) return 1 ;; esac
    ps_d=$(phys "$ps_one") || return 1
    tmp_under "$ps_d" || return 1
    ps_g=$(sgit -C "$ps_d" rev-parse --absolute-git-dir 2>/dev/null) || return 1   # the destination's own git directory (a .git file may lead elsewhere)
    ps_g=$(phys "$ps_g") || return 1
    tmp_under "$ps_g" || return 1
    ps_g=$(sgit -C "$ps_d" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || return 1
    ps_g=$(phys "$ps_g") || return 1
    tmp_under "$ps_g" || return 1
  done <<EOF
$ps_u
EOF
  return 0
}

do_git() {
  scope_args "$@"
  env_cfg
  # skip git's own options before the subcommand
  while [ $# -gt 0 ]; do
    case "$1" in
      -c|--config-env) bad_cfg "${2:-}"; shift 2 || return 0 ;;
      --config-env=*) bad_cfg "${1#--config-env=}"; shift ;;
      -C|--git-dir|--work-tree|--namespace|--exec-path) shift 2 || return 0 ;;
      -*) shift ;;
      *) break ;;
    esac
  done
  if [ "${1:-}" = config ]; then
    # PARSED LIKE GIT (2.46 and later): the options are only the words BEFORE the first word that is not an option; everything
    # after it is a positional (a value), whatever it starts with. DEFAULT DENY: a command passes only as a read (exactly one
    # read mode, or none, then the key words, the count matching that mode) or as the identity write; a word after the key that
    # starts with '-' is refused (an older git would read it as an option, a newer one as a value: either way it is not a read).
    shift; mode=""; pos=0; ok=1; wr=0; key=""; seen=0; skip=0
    for a in "$@"; do
      if [ "$skip" = 1 ]; then skip=0; continue; fi   # the value of --type or --default (before the key)
      if [ "$seen" = 1 ]; then
        case "$a" in -*) ok=0 ;; esac
        pos=$((pos + 1)); continue
      fi
      case "$a" in
        --rename-section|--remove-section|-e|--edit|--add|--unset|--unset-all|--replace-all) wr=1 ;;
        --get|--get-all|--get-regexp|--get-urlmatch|--list|-l|--get-color|--get-colorbool) if [ -z "$mode" ]; then mode=$a; else ok=0; fi ;;
        --global|--local|--system|--worktree|--show-origin|--show-scope|--name-only|-z|--includes|--no-includes) ;;
        --file|--file=*|-f|--blob|--blob=*) wr=1 ;;
        --type|--default) skip=1 ;;
        --type=*|--bool|--int|--bool-or-int|--path|--expiry-date|--no-type|--null|--default=*|--fixed-value) ;;   # read modifiers, before the key only
        -*) wr=1 ;;   # any other flag: git takes a unique prefix of a long option (--uns is --unset), so it is refused
        *) seen=1; pos=1
           key=$(printf '%s' "$a" | tr 'A-Z' 'a-z')
           case "$key" in
             edit|set|unset|rename-section|remove-section) wr=1 ;;   # the subcommand forms
             get|list) if [ -z "$mode" ]; then mode=$key; pos=0; seen=1; fi ;;   # the read subcommands: as --get, --list
           esac ;;
      esac
    done
    n_ok=0
    case "$mode" in
      --get|--get-all|--get-regexp|get) [ "$pos" = 1 ] && n_ok=1 ;;
      --list|-l|list) [ "$pos" = 0 ] && n_ok=1 ;;
      --get-urlmatch|--get-color|--get-colorbool) [ "$pos" -ge 1 ] && [ "$pos" -le 2 ] && n_ok=1 ;;
      "") if [ "$pos" = 1 ]; then n_ok=1; elif [ "$pos" = 2 ]; then case "$key" in user.name|user.email) n_ok=1 ;; esac; fi ;;
    esac
    [ "$wr" = 0 ] && [ "$ok" = 1 ] && [ "$n_ok" = 1 ] && return 0
    if in_tmp_scope; then cfg_scoped_ok "$@" && return 0; refuse "changing git configuration"; fi   # ask #2354: in scope, any key but the #CFGX ones
    set -- config "$@"
    for a in "$@"; do bad_cfg "$a"; done
    refuse "changing git configuration"
  fi
  [ "${1:-}" = push ] || return 0
  shift
  n=0; cur=""
  push_scoped=0; push_scope_check "$@" && push_scoped=1   # ask #2354: a local remote under /tmp, in a repository under /tmp
  for a in "$@"; do
    case "$a" in
      --mirror) refuse "force-pushing" ;;
      --force|--force-with-lease|--force-with-lease=*|--force-if-includes) [ "$push_scoped" = 1 ] || refuse "force-pushing" ;;
      --all|--branches) refuse "pushing to the default branch" ;;
      --*) ;;
      -*[!a-zA-Z]*) ;;
      -*f*) [ "$push_scoped" = 1 ] || refuse "force-pushing" ;;
    esac
  done
  skip=0
  for a in "$@"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$a" in
      --repo) skip=1; n=$((n + 1)); continue ;;   # the remote is given by the option
      --repo=*) n=$((n + 1)); continue ;;
      -o|--push-option|--receive-pack|--exec) skip=1; continue ;;
      -[!-]*o) skip=1; continue ;;   # a short cluster ending in o: the next word is its value
      --push-option=*|--receive-pack=*|--exec=*) continue ;;
      -*) continue ;;
    esac
    n=$((n + 1))
    [ "$n" -eq 1 ] && continue   # the remote
    case "$a" in *\**) refuse "pushing by a glob refspec" ;; esac
    case "$a" in +*) [ "$push_scoped" = 1 ] || refuse "force-pushing" ;; esac
    case "$a" in
      *:*) dst=${a#*:} ;;
      *) dst=$a ;;
    esac
    dst=${dst#refs/heads/}
    if [ "$dst" = HEAD ]; then
      cur=$("$real_git" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
      [ -n "$cur" ] && is_default "$cur" && [ "$push_scoped" != 1 ] && refuse "pushing to the default branch"
      continue
    fi
    is_default "$dst" && [ "$push_scoped" != 1 ] && refuse "pushing to the default branch"
  done
  if [ "$n" -lt 2 ]; then   # `git push` or `git push <remote>`: the current branch goes
    cur=$("$real_git" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    [ -n "$cur" ] && is_default "$cur" && [ "$push_scoped" != 1 ] && refuse "pushing to the default branch"
  fi
  return 0
}

api_path() {   # $1: the endpoint as given; prints the path to match: no scheme, host, port, userinfo, query, slashes at the ends
  p=$1
  case "$p" in
    *%*|*..*) refuse "a gh api endpoint it cannot read" ;;
    *://*)
      p=${p#*://}
      auth=${p%%[/?#]*}
      [ -z "$auth" ] && refuse "a gh api endpoint it cannot read"
      p=${p#"$auth"}
      ;;
  esac
  p=${p%%[?#]*}
  case "$p" in *:*) refuse "a gh api endpoint it cannot read" ;; esac
  p=$(printf '%s' "$p" | tr 'A-Z' 'a-z' | sed 's|^/*||; s|/*$||; s|^api/v3/||')
  printf '%s' "$p"
}

api_check() {   # gh api ...: $@ are the words after `api`
  method=""; path=""; body=0; take=""; gql_q=0; gql_bad=0
  for a in "$@"; do
    case "$take" in
      method) method=$a; take=""; continue ;;
      skip) take=""; continue ;;
      raw) body=1; take=""; case "$a" in query=*) gql_q=1 ;; esac; continue ;;
      typed) body=1; take=""; case "$a" in query=*) gql_bad=1 ;; esac; continue ;;
      input) body=1; gql_bad=1; take=""; continue ;;
    esac
    case "$a" in
      -[!-]?*)   # a cluster of short flags, read letter by letter: i (no value) or X H q t p f F (the rest of the cluster, or the next word, is the value)
        rest=${a#-}
        while [ -n "$rest" ]; do
          l=${rest%"${rest#?}"}; rest=${rest#?}
          case "$l" in
            i) continue ;;
            X|H|q|t|p|f|F)
              case "$l" in X) k=method ;; f) k=raw ;; F) k=typed ;; *) k=skip ;; esac
              if [ -z "$rest" ]; then take=$k
              else
                case "$k" in
                  method) method=$rest ;;
                  raw) body=1; case "$rest" in query=*) gql_q=1 ;; esac ;;
                  typed) body=1; case "$rest" in query=*) gql_bad=1 ;; esac ;;
                esac
              fi
              break ;;
            *) refuse "a gh api option cluster it cannot read" ;;
          esac
        done
        continue ;;
    esac
    case "$a" in
      -X|--method) take=method ;;
      -X?*) method=${a#-X} ;;
      --method=*) method=${a#--method=} ;;
      -H|--header|-q|--jq|-t|--template|--hostname|--cache|-p|--preview) take=skip ;;
      -f|--raw-field) take=raw ;;
      -F|--field) take=typed ;;
      --input) take=input ;;
      --input=*) body=1; gql_bad=1 ;;
      -f?*) body=1; v=${a#-f}; case "$v" in query=*) gql_q=1 ;; esac ;;
      --raw-field=*) body=1; v=${a#--raw-field=}; case "$v" in query=*) gql_q=1 ;; esac ;;
      -F?*) body=1; v=${a#-F}; case "$v" in query=*) gql_bad=1 ;; esac ;;
      --field=*) body=1; v=${a#--field=}; case "$v" in query=*) gql_bad=1 ;; esac ;;
      -*) ;;
      *) if [ -z "$path" ]; then path=$a; else refuse "a gh api call it cannot read (more than one endpoint)"; fi ;;
    esac
  done
  p=$(api_path "$path") || exit $?
  case "$*" in *mergePullRequest*|*enablePullRequestAutoMerge*) refuse "merging a pull request" ;; esac
  case "$*" in *mutation*) refuse "writing through GraphQL (merges, refs, issues, labels, settings)" ;; esac
  if [ "$p" = graphql ]; then   # only a query written on the command line (-f query=...) can be read here
    { [ "$gql_bad" = 1 ] || [ "$gql_q" != 1 ]; } && refuse "a GraphQL query it cannot read (from a file or stdin)"
  fi
  m=$(printf '%s' "$method" | tr 'a-z' 'A-Z')
  [ -z "$m" ] && { [ "$body" = 1 ] && m=POST || m=GET; }
  case "$p" in
    repos/*/pulls/*/merge|repos/*/merges) refuse "merging a pull request" ;;
  esac
  [ "$m" = GET ] && return 0
  case "$p" in
    repos/*/pulls/*/reviews|repos/*/pulls/*/reviews/*) refuse "approving or dismissing a pull request review" ;;
    repos/*/pulls/[0-9]*) [ "$m" = PATCH ] && refuse "pr-edit-api" ;;
    repos/*/issues/*|repos/*/issues) refuse "writing issues" ;;
    repos/*/labels|repos/*/labels/*) refuse "writing labels" ;;
    repos/*/git/refs|repos/*/git/refs/*) refuse "force-pushing or pushing to the default branch" ;;
    repos/*/dispatches|repos/*/actions/workflows/*/dispatches|repos/*/releases|repos/*/releases/*) refuse "starting workflows or releases" ;;
    orgs/*|user/*|users/*) refuse "changing repository settings" ;;
    repos/*/hooks|repos/*/hooks/*|repos/*/rulesets|repos/*/rulesets/*|repos/*/branches/*/protection*|repos/*/actions/secrets*|repos/*/dependabot/secrets*|repos/*/environments/*|repos/*/keys|repos/*/keys/*|repos/*/collaborators/*|repos/*/transfer|repos/*/variables*|repos/*/actions/variables*)
      refuse "changing repository settings" ;;
  esac
  case "$m" in PATCH|PUT|DELETE) case "$p" in repos/*) refuse "changing repository settings" ;; esac ;; esac
  return 0
}

pr_edit_check() {   # ask 1200: the words after `pr edit`: only `N --body-file F`, N one of the pod's own numbers when it keeps a record
  for a in "$@"; do
    case "$a" in
      --body|--body=*|-b|-b?*) refuse "pr-edit-inline-body" ;;
      *://*|-R|-R?*|--repo|--repo=*) refuse "pr-edit-url-other-repo" ;;
    esac
  done
  { [ $# -eq 3 ] && [ "$2" = --body-file ]; } || refuse "pr-edit-other-option"
  case "$1" in ""|*[!0-9]*) refuse "pr-edit-other-option" ;; esac
  { [ -n "${CW_PR_RECORD:-}" ] && [ -s "$CW_PR_RECORD" ]; } || refuse "pr-edit-no-record"   # fail closed: no record, no edit
  grep -qx -- "$1" "$CW_PR_RECORD" || refuse "pr-edit-other-number"
  return 0
}

do_gh() {
  while [ $# -gt 0 ]; do   # global flags before the group
    case "$1" in -R|--repo|--hostname) shift 2 || return 0 ;; -*) shift ;; *) break ;; esac
  done
  group=${1:-}; [ $# -gt 0 ] && shift
  case "$group" in   # a first word that is no known command could be an alias that expands to a refused one
    ""|agent-task|alias|api|attestation|auth|browse|cache|codespace|completion|config|copilot|extension|gist|gpg-key|help|issue|label|licenses|org|pr|preview|project|release|repo|ruleset|run|search|secret|ssh-key|status|variable|workflow|deploy-key) ;;
    *) refuse "a gh command it does not know (it could be an alias)" ;;
  esac
  [ "$group" = api ] && { api_check "$@"; return 0; }
  while [ $# -gt 0 ]; do
    case "$1" in -R|--repo) shift 2 || return 0 ;; -*) shift ;; *) break ;; esac
  done
  verb=${1:-}; [ $# -gt 0 ] && shift
  case "$group $verb" in
    "alias list") ;;
    "alias "*) refuse "defining or changing a gh alias" ;;
    "config get"|"config list") ;;
    "config "*) refuse "changing the gh configuration" ;;
    "pr merge") refuse "merging a pull request" ;;
    "pr review") refuse "approving or dismissing a pull request review" ;;
    "pr edit") for a in "$@"; do case "$a" in --add-label|--add-label=*|--remove-label|--remove-label=*) refuse "writing labels" ;; --add-assignee|--add-assignee=*|--remove-assignee|--remove-assignee=*) refuse "writing assignees" ;; esac; done; pr_edit_check "$@" ;;
    "issue create"|"issue edit"|"issue close"|"issue comment"|"issue reopen"|"issue delete"|"issue lock"|"issue unlock"|"issue pin"|"issue unpin"|"issue transfer")
      refuse "writing issues" ;;
    "label create"|"label edit"|"label delete"|"label clone") refuse "writing labels" ;;
    "workflow run"|"workflow enable"|"workflow disable"|"release create"|"release edit"|"release delete"|"release upload"|"run rerun"|"run cancel")
      refuse "starting workflows or releases" ;;
    "extension list"|"extension search"|"extension browse"|"extension ") ;;
    "extension "*) refuse "installing or running a gh extension" ;;
    "repo edit"|"repo delete"|"repo archive"|"repo unarchive"|"repo rename"|"secret set"|"secret delete"|"secret remove"|"variable set"|"variable delete"|"deploy-key add"|"deploy-key delete")
      refuse "changing repository settings" ;;
  esac
  return 0
}

case "$tool" in
  git) do_git "$@" ;;
  gh) do_gh "$@" ;;
  *) echo "cw-guard: unknown tool" >&2; exit 2 ;;
esac
