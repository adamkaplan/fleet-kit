#!/bin/sh
# cw-guard <gh|git> <args...>: the pod's convenience refusal of six kinds of command.
#   exit 0  the command is none of the six: the caller runs it unchanged
#   exit 3  refused: ONE line on stderr, "refused: <what>: ...". <what> is a fixed phrase; no argument value is ever printed.
#
# HONESTY: this is defence in depth and a convenience, NOT the guard. It runs only when the caller goes through the `gh` and
# `git` wrappers first on PATH; calling the real binary by its path, another HTTP client, or a script skips it. The guard is the
# repository's own rules (branch protection or a ruleset: no merge, no push to the default branch, no force-push) and the
# token's scope (preflight, #545). Do not rely on this file for either.
#
# Env: CW_REAL_GIT (the real git, used to read the current branch), CW_DEFAULT_BRANCH (extra default branch names,
# space or comma separated). `main` and `master` and the branch origin/HEAD points to are always treated as default.
tool=${1:-}; [ $# -gt 0 ] && shift
tail_msg="the repository's rules are the guard; this wrapper is only a convenience"
refuse() { echo "refused: $1: $tail_msg" >&2; exit 3; }
lower() { printf '%s' "$1" | tr 'A-Z' 'a-z'; }
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

do_git() {
  # skip git's own options before the subcommand
  while [ $# -gt 0 ]; do
    case "$1" in
      -C|-c|--git-dir|--work-tree|--namespace|--exec-path|--config-env) shift 2 || return 0 ;;
      -*) shift ;;
      *) break ;;
    esac
  done
  [ "${1:-}" = push ] || return 0
  shift
  n=0; cur=""
  for a in "$@"; do
    case "$a" in
      --force|--force-with-lease|--force-with-lease=*|--force-if-includes|--mirror) refuse "force-pushing" ;;
      --all|--branches) refuse "pushing to the default branch" ;;
      --*) ;;
      -*[!a-zA-Z]*) ;;
      -*f*) refuse "force-pushing" ;;
    esac
  done
  skip=0
  for a in "$@"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$a" in
      -o|--push-option|--receive-pack|--exec|--repo) skip=1; continue ;;
      -*) continue ;;
    esac
    n=$((n + 1))
    [ "$n" -eq 1 ] && continue   # the remote
    case "$a" in +*) refuse "force-pushing" ;; esac
    case "$a" in
      *:*) dst=${a#*:} ;;
      *) dst=$a ;;
    esac
    dst=${dst#refs/heads/}
    if [ "$dst" = HEAD ]; then
      cur=$("$real_git" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
      [ -n "$cur" ] && is_default "$cur" && refuse "pushing to the default branch"
      continue
    fi
    is_default "$dst" && refuse "pushing to the default branch"
  done
  if [ "$n" -lt 2 ]; then   # `git push` or `git push <remote>`: the current branch goes
    cur=$("$real_git" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    [ -n "$cur" ] && is_default "$cur" && refuse "pushing to the default branch"
  fi
  return 0
}

api_check() {   # gh api ...: $@ are the words after `api`
  method=""; path=""; body=0; take=""
  for a in "$@"; do
    if [ -n "$take" ]; then method=$a; take=""; continue; fi
    case "$a" in
      -X|--method) take=1 ;;
      -X?*) method=${a#-X} ;;
      --method=*) method=${a#--method=} ;;
      -f|-F|--field|--raw-field|--input|-f?*|-F?*|--field=*|--raw-field=*|--input=*) body=1 ;;
      -*) ;;
      *) [ -z "$path" ] && path=$a ;;
    esac
  done
  case "$*" in *mergePullRequest*|*enablePullRequestAutoMerge*) refuse "merging a pull request" ;; esac
  m=$(printf '%s' "$method" | tr 'a-z' 'A-Z')
  [ -z "$m" ] && { [ "$body" = 1 ] && m=POST || m=GET; }
  p=$(printf '%s' "$path" | sed 's|^/||; s|?.*||')
  case "$p" in
    repos/*/pulls/*/merge) refuse "merging a pull request" ;;
  esac
  [ "$m" = GET ] && return 0
  case "$p" in
    repos/*/issues/*|repos/*/issues) refuse "writing issues" ;;
    repos/*/labels|repos/*/labels/*) refuse "writing labels" ;;
    repos/*/git/refs/heads/*|repos/*/git/refs) [ "$m" != POST ] && refuse "force-pushing or pushing to the default branch" ;;
    repos/*/hooks|repos/*/hooks/*|repos/*/rulesets|repos/*/rulesets/*|repos/*/branches/*/protection*|repos/*/actions/secrets*|repos/*/dependabot/secrets*|repos/*/environments/*|repos/*/keys|repos/*/keys/*|repos/*/collaborators/*|repos/*/transfer|repos/*/variables*|repos/*/actions/variables*)
      refuse "changing repository settings" ;;
  esac
  case "$m" in PATCH|PUT|DELETE) case "$p" in repos/*) refuse "changing repository settings" ;; esac ;; esac
  return 0
}

do_gh() {
  while [ $# -gt 0 ]; do   # global flags before the group
    case "$1" in -R|--repo|--hostname) shift 2 || return 0 ;; -*) shift ;; *) break ;; esac
  done
  group=${1:-}; [ $# -gt 0 ] && shift
  [ "$group" = api ] && { api_check "$@"; return 0; }
  while [ $# -gt 0 ]; do
    case "$1" in -R|--repo) shift 2 || return 0 ;; -*) shift ;; *) break ;; esac
  done
  verb=${1:-}; [ $# -gt 0 ] && shift
  case "$group $verb" in
    "pr merge") refuse "merging a pull request" ;;
    "pr edit") for a in "$@"; do case "$a" in --add-label|--add-label=*|--remove-label|--remove-label=*) refuse "writing labels" ;; esac; done ;;
    "issue create"|"issue edit"|"issue close"|"issue comment"|"issue reopen"|"issue delete"|"issue lock"|"issue unlock"|"issue pin"|"issue unpin"|"issue transfer")
      refuse "writing issues" ;;
    "label create"|"label edit"|"label delete"|"label clone") refuse "writing labels" ;;
    "repo edit"|"repo delete"|"repo archive"|"repo unarchive"|"repo rename"|"secret set"|"secret delete"|"secret remove"|"variable set"|"variable delete"|"ruleset"*|"deploy-key add"|"deploy-key delete")
      refuse "changing repository settings" ;;
  esac
  return 0
}

case "$tool" in
  git) do_git "$@" ;;
  gh) do_gh "$@" ;;
  *) echo "cw-guard: unknown tool" >&2; exit 2 ;;
esac
