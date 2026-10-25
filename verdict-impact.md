verdict: approve
head: 37e6d6745e90293e3a7854038534644ffad9799d
impact-action: second-review
impact-class: authority path (cloud hub relay: pod-to-ledger writes via the daemon; cloud_relay_daemon, cloud_hub, cloud_relay are listed in impact-matrix authority)
impact-severity: Critical
impact-probability: Remote
impact-reason: new pod-reachable path writes plan-ledger proofs/verdicts through the daemon; gated by pod token, repo binding, holder config, evaluator registry, live-head recheck, replay 409; wrong gating would corrupt gate evidence, hence second-review
files-read:
- bin/cloud_relay.py | 1-245 | 245
- cloud/worker/bin/cloud_relay.py | 1-245 (diff vs bin copy: identical) | 245
- bin/cloud_relay_daemon.py | 1-403 | 403
- bin/cloud_hub.py | changed hunks (diff) | not counted whole
- cloud/worker/bin/cloud_hub.py | identical to bin copy by diff | not counted
- bin/cloud_cli.py | changed hunks (diff) | not counted whole
- cloud/worker/bin/cloud_cli.py | identical to bin copy by diff | not counted
- bin/fleet-switchboard | 6023-6060 (run_daemon, only changed hunk; caller of pass_if_configured) and load_module 123-131 | 27740
- cloud/worker/Dockerfile | diff hunk | not counted
- docs/review-laws/impact-matrix.json | diff hunks and 150-180 | 576
- bin/cloud_channel_server.py | 40-80 (MAX_HANDLERS=32, slot semaphore) | not counted
- NOT read: bin/test-cloud-hub, bin/test-cloud-hub-relay, bin/test-cloud-relay, bin/test-cloud-relay-daemon, bin/test-cloud-launcher, cloud/launcher/tests/*, docs/cloud-channel.md, plan-1225.md
findings:
- IMPACT-SAT | note | bin/cloud_hub.py:_relay_post | At real constants (MAX_HANDLERS=32, pod relay admitted <= 16, wait cap min(16,16)) daemon routes are reachable against pod RELAY saturation. But channel POSTs and /decisions are not admitted/limited, so a pod flooding slow channel requests can still take the other 16 slots; code comment admits this | optionally reserve slots for daemon routes explicitly
- IMPACT-POLL | note | bin/cloud_relay_daemon.py:pass_if_configured / bin/fleet-switchboard:6052 | relay pulled only once per engine pass, synchronously after run_pass; if poll interval exceeds hub WAIT (30s) pods get 504 routinely; pass blocks up to 10 requests x 20s | document that poll must be < 30s or poll relay separately
- IMPACT-TEST | note | tests | test files not read by me; saturation behaviour not verified by running (read-only role) | n/a
