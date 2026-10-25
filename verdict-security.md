verdict: approve
head: 37e6d6745e90293e3a7854038534644ffad9799d
findings:
- sec-capacity | note | bin/cloud_hub.py:343 | Pod relay handlers are capped at max_handlers//2 (16 of the real MAX_HANDLERS=32; held waits 4 per pod, 16 total), so pod saturation alone cannot lock the daemon routes out (/relay/pending, /relay/result take no relay slot). The other 16 slots are shared with /message, /decisions and unauthenticated slow connections, which are counted before auth. 16 slow clients of those can still crowd the daemon out for the 5 s read timeout and the total deadline. The author documents this. | Reserve a slot or semaphore for the daemon routes, or accept it, since the daemon retries every pass and entries expire after the 120 s TTL.
- sec-link | note | bin/cloud_relay_daemon.py:236 | The PR-to-issue link comes from the PR body's closing keywords, which any writer in the repo can edit, including a pod holding pull-requests:write. This is documented as not a boundary; the real checks are relay_holders for prove and plan_evaluators and independence for verdict, both daemon-side and fail-closed. | Keep the docs wording; optionally also require the PR author or branch to match the pod.
- sec-reason | note | bin/cloud_relay_daemon.py:182 | Reasons returned to pods are fixed or allow-listed, except the verbatim `why` from plan_ledger.may_record_verdict, the evaluator name, and a 12-character head prefix in the stale-head refusal. Raw errors (the 500 and LedgerError paths) go only to the audit. No secret reaches a reason; a verdict refusal may name the proof authors. | Fine as is; consider fixed text for may_record_verdict reasons.
- sec-memory | note | bin/cloud_relay.py:44 | The hub keeps up to MAX_DONE=1000 answered entries, each holding a call of up to 16 args of 8 KB, so about 128 MB worst case. A hub replay fingerprint covers command, pr and head but not args, so a replay with different args gets the first answer (harmless, as the daemon is the source of truth). | Optionally drop `call` from finished entries.
- sec-ok | note | bin/cloud_hub.py:300 | Checked and sound: identity comes only from the token review (body pod/repo/issue are ignored; the daemon trusts only the hub-resolved fields); the daemon is denied as a pod and is the only caller of the daemon routes; the secret is never logged; every refusal is audited and writes no ledger entry; the daemon client sends no redirect, refuses plain http off-cluster and reads capped files; copies of cloud_cli, cloud_hub and cloud_relay are byte-identical. | none

impact:
files-read:
- bin/cloud_relay.py | 245 | 245
- bin/cloud_relay_daemon.py | 403 | 403
- bin/cloud_hub.py | 483 | 483
- bin/cloud_cli.py | 186 | 186
- bin/cloud_channel_server.py | 85 | 178
- bin/fleet-switchboard | 6049 | 27740
- cloud/worker/bin/cloud_relay.py | 245 | 245
- cloud/worker/bin/cloud_hub.py | 483 | 483
- cloud/worker/bin/cloud_cli.py | 186 | 186
- cloud/worker/Dockerfile | 99 | 99
- docs/review-laws/impact-matrix.json | 576 | 576
- bin/test-cloud-launcher | 19 | 19
- bin/test-cloud-hub | 11 | 939
- cloud/launcher/tests/test_cw_names.py | 2 | 166
- cloud/launcher/tests/test_orchestrator_agent.py | 5 | 481
Not read: bin/test-cloud-relay, bin/test-cloud-hub-relay, bin/test-cloud-relay-daemon (tests), docs/cloud-channel.md. fleet-switchboard: the single changed hunk (one added line in run_daemon, line 6049-6052) plus the report method (5203-5243), not the whole function; the real caller of pass_if_configured is run_daemon. The cloud_cli, cloud_hub and cloud_channel_server line counts are approximate (the files were read in parts through the diff and sed ranges).
