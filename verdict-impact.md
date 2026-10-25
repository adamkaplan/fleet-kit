verdict: approve
head: 02d8e85dd39ae862eba81af755002992fb628af0
impact-action: hold
impact-class: authority
impact-severity: Critical
impact-probability: Remote
impact-reason: a pod can now cause ledger proof/verdict writes through the hub and daemon (top tier authority path; the matrix cell Critical/Remote is Serious, action H); the guards read (secret-bound pod and repo, PR link, holder and evaluator checks, head re-read under the ledger lock, reserved daemon name, caps) make misuse unlikely
files-read:
- docs/review-laws/impact-matrix.json | diff plus lines 1-60, cells Critical/Marginal, owner_hold | 576
- docs/review-laws/SECURITY_STANDARDS.md | 1-20 | 20
- docs/review-laws/structural-rule.md | 1-5 | 5
- docs/review-laws/REVIEW_LAWS.md | grep of impact lines only | 87
- bin/cloud_relay.py | 1-199 | 199
- bin/cloud_relay_daemon.py | 1-399 | 399
- bin/cloud_hub.py | diff hunks only | not counted
- bin/cloud_cli.py | diff hunks only | not counted
- bin/fleet-switchboard | diff hunk only (1 line) | not counted
- cloud/worker/bin/cloud_relay.py | byte-compared equal to bin/cloud_relay.py | 199
findings:
- R-LAW-10 | note | bin/cloud_relay_daemon.py:357 | the PR-to-issue link comes from the PR body, which the file's own docstring says is not a boundary; the real boundaries are relay_holders and plan_evaluators | keep that documented and keep the config entries for holders and evaluators reviewed
- R-LAW-2 | note | tests | the tests (bin/test-cloud-relay*, bin/test-cloud-hub-relay) and the docs were not read, so their coverage is not judged | none
