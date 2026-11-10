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
# THE git PUSH TABLE (the one place the forms are listed): `#PUSH <refused|allowed> | <form> | <example args> | <why>`. The test runs every
# row through the wrapper, and checks that the README paragraph names exactly these forms. The wrapper's git part is this ONE rule: a
# `git push` to a NETWORK remote (https, http, ssh, git, scp-like; a named remote is read with `git remote get-url --push`) whose destination
# is the default branch. Every other git command, and every push to a local path or file:// remote or of another branch, passes unchecked.
#PUSH refused | a refspec naming the default branch | origin HEAD:main | the destination is the default branch
#PUSH refused | a glob refspec that matches the default branch | origin refs/heads/*:refs/heads/* | a glob can reach the default branch
#PUSH refused | --all, --branches or --mirror | --all origin | every branch goes, the default branch with them
#PUSH refused | a bare branch word that is the default branch | origin main | the destination is the default branch
#PUSH refused | no refspec while the current branch is the default branch | origin | the current branch goes
#PUSH refused | a sha form that is not the exact land form | origin abc1234:refs/heads/main | a short sha, a leading plus, --force or --force-with-lease
#PUSH refused | the land form without both land trailers | origin SHA:refs/heads/main | no Land-Checks or no Land-Council trailer, one empty, or the text not recorded
#PUSH allowed | the land form with both land trailers | origin SHA:refs/heads/main | exactly 40 hexadecimal characters, a non-empty Land-Checks and Land-Council, none not recorded
#PUSH allowed | a push to a local path or file:// remote | --force /tmp/x main | not a network remote
#PUSH allowed | a push of another branch, forced or not | --force origin feature | not the default branch
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

net_remote() {   # $1: a remote name or URL: success when it is a network remote
  r=$1
  case "$r" in
    https://*|http://*|ssh://*|git://*) return 0 ;;
    file://*|/*|./*|../*) return 1 ;;
  esac
  u=$("$real_git" remote get-url --push -- "$r" 2>/dev/null) || u=$r   # not a remote name: a path (or a URL) as written
  case "$u" in
    https://*|http://*|ssh://*|git://*) return 0 ;;
    file://*|/*|./*|../*) return 1 ;;
    *:*) case "${u%%:*}" in */*) return 1 ;; *) return 0 ;; esac ;;   # scp-like [user@]host:path (a slash before the colon is a path)
  esac
  return 1
}

land_record() {   # $1: the sha, the exact commit: both land trailers present, non-empty, none "not recorded"
  [ "$("$real_git" rev-parse --verify --quiet "$1^{commit}" 2>/dev/null)" = "$1" ] || return 1
  tr_out=$("$real_git" log -1 --format=%B "$1" 2>/dev/null | "$real_git" interpret-trailers --parse 2>/dev/null) || return 1
  for t in Land-Checks Land-Council; do
    vals=$(printf '%s\n' "$tr_out" | sed -n "s/^$t:[[:space:]]*//p" | sed 's/[[:space:]]*$//')
    [ -n "$(printf '%s\n' "$tr_out" | grep "^$t:")" ] || return 1
    while IFS= read -r v; do
      [ -n "$v" ] || return 1
      [ "$(printf '%s' "$v" | tr 'A-Z' 'a-z')" = "not recorded" ] && return 1
    done <<EOF
$vals
EOF
  done
  return 0
}

do_git() {
  # skip git's own options before the subcommand; -C is followed (the guard changes its own directory), --git-dir is exported
  while [ $# -gt 0 ]; do
    case "$1" in
      -C) cd "${2:-.}" 2>/dev/null || return 0; shift 2 || return 0 ;;
      --git-dir) GIT_DIR=${2:-}; export GIT_DIR; shift 2 || return 0 ;;
      --git-dir=*) GIT_DIR=${1#--git-dir=}; export GIT_DIR; shift ;;
      -c|--config-env|--work-tree|--namespace|--exec-path) shift 2 || return 0 ;;
      -*) shift ;;
      *) break ;;
    esac
  done
  [ "${1:-}" = push ] || return 0
  shift
  set -f   # no pathname expansion of the unquoted words below
  forced=0; wide=0; remote=""; repoopt=""; specs=""; skip=0; rnext=0; first=""
  for a in "$@"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    if [ "$rnext" = 1 ]; then rnext=0; repoopt=$a; continue; fi
    case "$a" in
      --repo) rnext=1; continue ;;
      --repo=*) repoopt=${a#--repo=}; continue ;;
      -o|--push-option|--receive-pack|--exec|--recurse-submodules) skip=1; continue ;;
      --push-option=*|--receive-pack=*|--exec=*) continue ;;
      --force|--force-with-lease|--force-with-lease=*|--force-if-includes) forced=1; continue ;;
      --all|--branches|--mirror) wide=1; continue ;;
      --*) continue ;;
      -[!-]*) case "$a" in *f*) forced=1 ;; esac; case "$a" in *o) skip=1 ;; esac; continue ;;   # a short cluster; ending in o: the next word is its value
    esac
    if [ -z "$first" ]; then first=$a; fi
    specs="$specs
$a"
  done
  cur=$("$real_git" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
  # git takes the first positional word as the repository, whatever --repo says: with --repo the guard judges both words as remotes and
  # every positional word as a refspec; without a repository word the branch's remote (else origin) is the remote
  if [ -n "$repoopt" ]; then
    net_remote "$repoopt" || { [ -n "$first" ] && net_remote "$first"; } || return 0
  else
    if [ -n "$first" ]; then
      remote=$first
      specs=${specs#"
$first"}
    else
      remote=$("$real_git" config --get "branch.$cur.pushRemote" 2>/dev/null || "$real_git" config --get "branch.$cur.remote" 2>/dev/null || echo origin)
    fi
    net_remote "$remote" || return 0
  fi
  hit=0; land=""; cnt=0
  [ "$wide" = 1 ] && hit=1
  while IFS= read -r a; do
    [ -n "$a" ] || continue
    cnt=$((cnt + 1))
    plus=0; case "$a" in +*) plus=1; a=${a#+} ;; esac
    case "$a" in *:*) dst=${a#*:}; src=${a%%:*} ;; *) dst=$a; src=$a ;; esac
    [ "$a" = : ] && hit=1   # the matching push: every branch that exists on both sides
    dst=${dst#refs/}; dst=${dst#heads/}   # git also takes heads/<name> for refs/heads/<name>
    case "$dst" in
      *\**|*\?*|*\[*) for d in $(defaults); do case "$d" in $dst) hit=1 ;; esac; done; continue ;;
    esac
    if [ "$dst" = HEAD ]; then
      [ -n "$cur" ] && is_default "$cur" && hit=1
      continue
    fi
    if is_default "$dst"; then
      hit=1
      if [ "$plus" = 0 ] && [ "$a" = "$src:refs/heads/$dst" ]; then land=$src; fi
    fi
  done <<EOF
$specs
EOF
  if [ "$cnt" -lt 1 ]; then   # `git push` or `git push <remote>`: the current branch goes
    [ -n "$cur" ] && is_default "$cur" && hit=1
  fi
  [ "$hit" = 1 ] || return 0
  # the one exception: exactly <40-hex>:refs/heads/<default>, one refspec, no force option, a land record on that commit
  if [ "$cnt" = 1 ] && [ "$forced" = 0 ] && [ "$wide" = 0 ] && [ -n "$land" ]; then
    case "$land" in *[!0-9a-f]*) ;; *) [ ${#land} -eq 40 ] && land_record "$land" && return 0 ;; esac
  fi
  refuse "pushing to the default branch of a network remote"
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
