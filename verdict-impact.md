verdict: incomplete
head: 2391ad7495214b279daa9880a50dfad7df9856bf
impact-action: second-review
impact-class: top-tier authority path (ledger writes via the relay daemon; hub auth and pod-binding), per the matrix entries this PR adds for bin/cloud_relay_daemon.py, bin/cloud_hub.py, bin/cloud_relay.py
impact-severity: Critical
impact-probability: Remote
impact-reason: relay holds ledger-writing authority; the wait caps are fixed constants and do not provably keep the daemon routes reachable under slow-request saturation. I did not read every changed file, so this is a partial rating.
files-read:
- bin/cloud_hub.py | 1-452 | 452
- bin/cloud_relay.py | 1-217 | 217
- bin/cloud_relay_daemon.py | 1-403 | 403
- bin/cloud_cli.py | diff hunks only (57 changed lines), not the whole file | 186
- bin/fleet-switchboard | diff hunk only (1 added line, ~6049), not the whole file | 30645
- docs/cloud-channel.md | diff hunk only | 190
- docs/review-laws/impact-matrix.json | diff hunks only (9 added lines); the matrix body was not read | 576
- cloud/worker/Dockerfile | diff hunk only | 80
- cloud/launcher/tests/test_cw_names.py | diff hunk only | 165
- cloud/launcher/tests/test_orchestrator_agent.py | diff hunks only | 481
- bin/test-cloud-launcher | diff hunk only | 17
- bin/test-cloud-hub | diff hunk only | 795
- bin/test-cloud-hub-relay | grep of saturation lines only (about 270-310) | 383
- bin/test-cloud-relay | not read | 696
- bin/test-cloud-relay-daemon | not read | 571
- cloud/worker/bin/cloud_hub.py, cloud/worker/bin/cloud_relay.py, cloud/worker/bin/cloud_cli.py | byte-identical to bin/ (cmp clean) | 452, 217, 186
findings:
- saturation | block | bin/cloud_relay.py:41 | MAX_WAIT_TOTAL=16 is a fixed constant. It is not derived from the server's max_handlers (cloud_hub.make_hub(max_handlers=...), default 32), and nothing checks MAX_WAIT_TOTAL <= max_handlers/2. With max_handlers <= 16 the held waits can take every slot and starve /relay/pending and /relay/result. | derive the cap from the server's real max_handlers in make_hub (or raise ValueError when it is not at most half), and test it.
- saturation | block | bin/cloud_hub.py:~215 (_relay_post, _body, _caller) | Only held waits are counted. A handler stuck in _caller (a TokenReview of up to 10 s) or in _body (a slow-body read, which times out only on the socket) holds a slot without counting against the cap. Slow clients can fill the remaining 16 slots, so the daemon's pull and result routes are not guaranteed under saturation. The docs and comments claim it is guaranteed. | reserve slots for the daemon (a separate semaphore, or admit the daemon credential first), or count all in-flight relay handlers. Add a test with 16 held waits plus 16 slow bodies and show GET /relay/pending still answers.
- cap-order | note | bin/cloud_relay.py:~112 | The per-pod and total wait-cap checks run before the entry lookup, so a replay of an already queued, unanswered call from a pod at its cap gets 429. That is safe but surprising. | document it.
- 409-path | note | bin/cloud_relay.py:~108 | The 409 reply returns before the cap check; there is no leak. | none
- evidence | note | bin/cloud_relay_daemon.py:21 | Pod evidence is stored unscrubbed in the ledger (stated in the code). | keep the doc warning; consider scrubbing.
- coverage | note | n/a | I did not read bin/test-cloud-relay, bin/test-cloud-relay-daemon, or the full test-cloud-hub-relay, cloud_cli.py, fleet-switchboard or impact-matrix.json. I could not confirm the claimed test of saturation (test-cloud-hub-relay:275-310 sets max_wait_total=6 and does not run at the real constants or against slow bodies). | a full read by another reviewer.
