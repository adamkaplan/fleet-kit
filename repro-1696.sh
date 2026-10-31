#!/bin/bash
# repro-1696.sh: run the gh shim of origin/main and of origin/integration in front of a FAKE real gh and print what would be posted.
# Scratch file for plan-1696; not merged. Usage: bash repro-1696.sh   (run from a clone of fleet-kit; needs git and python3; no network)
set -u
REPO=$(git rev-parse --show-toplevel)
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
mkdir -p "$W/realbin"
cat > "$W/realbin/gh" <<'PY'
#!/usr/bin/env python3
import json, os, sys
files = {}
for a in sys.argv[1:]:
    for p in (a, a.split("=@", 1)[-1], a[2:]):
        if os.path.isfile(p):
            files[p] = open(p).read()
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
print("  POSTED argv=%s stdin=%s files=%s" % (json.dumps(sys.argv[1:]), json.dumps(stdin), json.dumps(files)))
PY
chmod +x "$W/realbin/gh"
cat > "$W/fakesb" <<'SH'
#!/bin/sh
if [ "$1" = whoami ]; then
  echo '{"name": "alice-bot", "repo": "acme-org/widget-api"}'; exit 0
fi
exit 2
SH
chmod +x "$W/fakesb"
mkdir -p "$W/state"
printf 'from a file\n' > "$W/body.txt"
printf '{"body":"hello"}' > "$W/in.json"
for ref in main integration; do
  mkdir -p "$W/$ref"
  git -C "$REPO" archive "origin/$ref" shims | tar -x -C "$W/$ref"
done
run() {  # run <label> <stdin-or-NONE> args...
  local label=$1 in=$2; shift 2
  for ref in main integration; do
    echo "[$ref] $label"
    if [ "$in" = NONE ]; then inp=/dev/null; else inp=$W/stdin.txt; printf '%s' "$in" > "$inp"; fi
    (cd "$W" && env -i PATH="$W/$ref/shims:$W/realbin:/usr/bin:/bin" HOME="$W" TMPDIR="$W" FLEET_SWITCHBOARD_STATE="$W/state" \
      FLEET_SWITCHBOARD_BIN="$W/fakesb" FLEET_SHIM_LOOKUP_SECONDS=10 \
      python3 "$W/$ref/shims/gh" "$@" < "$inp" 2>&1 | sed 's/^/  /'; echo "  exit=${PIPESTATUS[0]}")
  done
}
E=repos/acme-org/widget-api
run "a  api issues/7/comments -f body=hello" NONE api $E/issues/7/comments -f body=hello
run "b  api -F body=@file" NONE api $E/issues/7/comments -F body=@$W/body.txt
run "c  api -X PATCH issues/comments/123" NONE api $E/issues/comments/123 -X PATCH -f body=hello
run "d  api --input file.json" NONE api $E/issues/7/comments --input $W/in.json
run "e  api --input - (stdin)" '{"body":"hello"}' api $E/issues/7/comments --input -
run "d2 api reviews --input file.json" NONE api $E/pulls/7/reviews --input $W/in.json
for m in addComment addPullRequestReview addPullRequestReviewComment addPullRequestReviewThread addPullRequestReviewThreadReply updateIssueComment updatePullRequestReview updatePullRequestReviewComment createIssue updateIssue createPullRequest updatePullRequest closeIssue addDiscussionComment createDiscussion updateDiscussionComment createCommitComment updateCommitComment; do
  run "f/g graphql $m" NONE api graphql -f "query=mutation{$m(input:{subjectId:\"X\",body:\"hello\"}){clientMutationId}}"
done
run "g2 graphql query from file (-F query=@file)" NONE api graphql -F query=@$W/body.txt
run "g3 graphql mergePullRequest (U: merge mutation)" NONE api graphql -f 'query=mutation{mergePullRequest(input:{pullRequestId:"X"}){clientMutationId}}'
run "g4 graphql read (U)" NONE api graphql -f 'query=query{viewer{login}}'
run "h1 issue close 7 --comment hello" NONE issue close 7 --comment hello
run "h2 pr close 7 --comment hello" NONE pr close 7 --comment hello
run "i1 -R api comments -f body" NONE -R acme-org/widget-api api $E/issues/7/comments -f body=hello
run "i2 -R issue comment" NONE -R acme-org/widget-api issue comment 7 --body hello
run "i3 --repo= pr close -c" NONE --repo=acme-org/widget-api pr close 7 -c hello
run "j  api pulls/comments/5 PATCH (known limit)" NONE api $E/pulls/comments/5 -X PATCH -f body=hello
run "j2 api pulls/7/comments/5/replies (known limit)" NONE api $E/pulls/7/comments/5/replies -f body=hello
run "U1 api read GET issues/7/comments" NONE api $E/issues/7/comments
run "U2 pr comment 7 --body hello" NONE pr comment 7 --body hello
