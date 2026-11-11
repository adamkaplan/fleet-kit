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
# `gh` first word it does not know is refused (it could be an alias); a glob refspec that matches the default branch is refused. A pod
# started without a git-token gets no wrappers at all: then the OpenCode deny rules are the only pod-side layer.
# Env: CW_REAL_GIT (the real git, used to read the current branch), CW_DEFAULT_BRANCH (extra default branch names,
# space or comma separated). `main` and `master` and the branch origin/HEAD points to are always treated as default.
# THE git part: none. The push rule (the default branch of a network remote needs a land record) is the image's pre-push hook,
# cloud/worker/pod-hooks/pre-push, installed as the global core.hooksPath; every git command passes through this wrapper unchecked.
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

do_git() { return 0; }

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
