#!/usr/bin/env python3
"""A stand-in for git in the end hook's tests (#1354): every call succeeds and prints nothing (a `log` names one unpushed commit), except a push or a fetch, which is refused with
the answer FAKE_GIT_SAYS names (a fixed set below). Nothing here touches a network or a repository."""
import os
import sys

ANSWERS = {
    "ended": "fatal: Authentication failed for 'https://github.com/acme-org/widget-service.git/'",
    "rejected": " ! [remote rejected] HEAD -> w1/wip (protected branch hook declined)",
    "network": "fatal: unable to access 'https://github.com/acme-org/widget-service.git/': Could not resolve host: github.com",
}
args = sys.argv[1:]
verb = next((a for a in args if a in ("push", "fetch", "status", "branch", "log")), "")
say = os.environ.get("FAKE_GIT_SAYS", "")
if verb in ("push", "fetch") and say in ANSWERS and (verb == os.environ.get("FAKE_GIT_VERB", "push")):
    sys.stderr.write(ANSWERS[say] + "\n")
    sys.exit(1)
if verb == "log":   # #1579: HEAD holds one commit beyond every remote-tracking ref (the hook decides 'nothing to push' from `git log`)
    print("0123456789abcdef0123456789abcdef01234567")
sys.exit(0)
