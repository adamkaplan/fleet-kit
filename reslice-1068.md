# Re-slice of ask #1068 (orchestrator role, persistent mode, token role): plan only

Source: origin/cw/orchestrator-role (PR #1159, closed). Merge-base 70e17d4f; 20 commits, +1284/-82, 18 files.
Integration has moved 165 commits since; a trial merge conflicts in 4 files:
bin/cloud_worker.py, bin/test-cloud-launcher, cloud/launcher/cloud_worker.py, docs/cloud-operator-quickstart.md.
Do not merge the old branch. Re-apply by slice (cherry-pick, resolve, or rewrite as noted).

## 1. Diff grouped by concern (merge-base..branch)
- Launcher code: cloud/launcher/cloud_worker.py +210/-39; cloud/launcher/app_token.py +35/-18; bin/cloud_worker.py +15/-5.
- Pod spec / manifests: StatefulSet rendering lives in cloud_worker.py; cloud/k8s/operator-rbac.yaml +5 (statefulsets, apps).
- Entrypoint: none touched by the branch. Not touched either: cloud/launcher/preflight.py, cloud/token-table.txt (the plan wants both to read the table; that part is NEW work).
- Tests: test_cloud_orchestrator.py +709 (new), test_cloud_statefulset_lookup.py +121, test_cloud_statefulset_strict.py +51, test_cloud_shapes.py +51/-12, fake_kubectl +16/-1, small edits in test_cloud_k1/pod_deadline/settings/worker/issues_read, bin/test-cloud-launcher.
- Docs: cloud/README.md +13, cloud/launcher/README.md +23/-1, docs/cloud-operator-quickstart.md +15.

## 2. What moved on integration (git log 70e17d4f..origin/integration -- path)
- cloud/launcher/cloud_worker.py (6 commits): 293a6b83 (plan check, retire frees name), 8eca10e8 (#1144 push unpushed work at stop/retire), fb33c715 (#1063), b79504ea (#1232 stack model credential), 444a8764 (#1231 stack image pull identity), 4be1dfc7 (#1065 reaper login). The stop/retire/reap paths the branch edits changed under it.
- bin/cloud_worker.py: 17 commits. app_token.py, preflight.py, token-table.txt, fake_kubectl: 1 each. operator-rbac.yaml: 0 (applies clean).
- XS already landed: SHAPES has XS/S/M (cloud_worker.py:38), DEFAULT_SHAPE="XS". Integration ALREADY has `DEFAULT_ORCHESTRATOR_SHAPE = "S"` and `--agent orchestrator` (line ~42, 271, 1582): an orchestrator launch exists as a plain worker pod. The plan says role comes from `--role`; check how `--agent` and `--role` relate before slice 1 (Q1).

## 3. Slices (strict order 1 -> 2 -> 3 -> 4; 5 optional/blocked)

### Slice 1: role table + ORCH shape row (not held; no credential or pod-spec change)
- Intent: one role table (role -> shape, token role, persistent) and the ORCH row (250m/768Mi, no runtime), read by launcher, preflight and docs; refusals S3 before any call.
- Files: cloud/launcher/cloud_worker.py, cloud/launcher/preflight.py, bin/cloud_worker.py, tests test_cloud_shapes.py, (new) test_cloud_roles.py, bin/test-cloud-launcher, cloud/launcher/README.md.
- Size: ~170 lines (+tests ~120).
- Covers: S2, S3, U1 (docs half of S11 deferred to slice 4). Needs: nothing (after batch 7).
- Held: no, if the role only picks size and still renders a plain worker pod. Keep the persistent column unused here.
- Start: cherry-pick 910aeb48 and 971c74f7 with -n, keep only role/SHAPES/shape-choices hunks, drop StatefulSet and token hunks; 934eae05 (shape tests) applies on top. Rewrite onto the current SHAPES (XS exists; add ORCH sibling) and reconcile with DEFAULT_ORCHESTRATOR_SHAPE/--agent. New: preflight reads the row.

### Slice 2: token role table (HELD: credentials path)
- Intent: app_token.py role -> exact permission set; worker row unchanged (EXPECTED_PERMISSIONS); orchestrator = issues write, pulls read, checks read, metadata read; exact-check and owner-step refusal rendered from the table; renewal takes role from the worker record, not the annotation.
- Files: cloud/launcher/app_token.py, cloud/token-table.txt, cloud/launcher/cloud_worker.py (mint and renew call sites only), tests test_cloud_orchestrator.py (token part), test_issues_read.py.
- Size: ~120 + tests ~200. Covers: S7, S9, S8 (annotation flip), U1. Needs: slice 1 (role value).
- Held: YES, `merge-captain` + `hold-credential`, `MERGE: captain` in first 3 lines, owner step named in body (PC-2).
- Start: take app_token.py hunks of 910aeb48 (1 commit on integration since: re-diff by hand); rewrite token-table.txt rendering (branch never touched it); ecc6d614 (CW_GIT_TOKEN_ROLE not-a-setting) and the test_cloud_settings.py line.

### Slice 3: persistent StatefulSet, stop, reaper rule (HELD: pod spec)
- Intent: role orchestrator renders a 1-replica StatefulSet (restart Always, no deadline, no activeDeadlineSeconds, per-run Secret owned by the StatefulSet, no key/endpoint/workload-identity label); stop/delete remove workload first; reaper skips deadline and idle for it and still renews tokens.
- Files: cloud/launcher/cloud_worker.py (render, create, stop, delete, reap), cloud/k8s/operator-rbac.yaml, cloud/launcher/tests/{fake_kubectl,test_cloud_orchestrator.py,test_cloud_statefulset_lookup.py,test_cloud_statefulset_strict.py,test_cloud_pod_deadline.py,test_cloud_k1.py,test_cloud_worker.py}, bin/test-cloud-launcher.
- Size: ~220 code + ~600 tests. Covers: S4, S5, S6, S8, S10 (3 mutants), U1, U3. Needs: slices 1 and 2.
- Held: YES, same labels; spec and Secret are the credentials path; RBAC grant for statefulsets is the owner's cluster change.
- Start: cherry-pick in order 910aeb48 (persistent part), 3cb78be0, b39c74e4, f2141ad4, 17dc6134, ad1ea318, b6659888, b7afddf4, 5efdcf74, b4551841, d29559f3, f3d57951. Skip merge commits (b874b61b, c2509592, 4a793378, 861c99ae). Conflicts expected in stop/retire/reap from 8eca10e8 (pod pushes work at stop: the StatefulSet delete must come after it, and the push-in-pod must still find its pod), 4be1dfc7, 293a6b83 (name freed after retire). Plan on a rewrite of the stop path, reuse the tests.

### Slice 4: docs and cost per day (not held)
- Intent: role, mode, token set per role, and cost/day = 24 x row usd_per_hour (0.25 CPU/768Mi row, ~$0.5/day on list price), generated from the table; test S11.
- Files: cloud/README.md, cloud/launcher/README.md, docs/cloud-operator-quickstart.md, token-table.txt render, one test. Size: ~70 lines. Covers: S11, rest of S2. Needs: slices 1-3 (for the facts).
- Start: take doc hunks from 910aeb48, 3cb78be0; quickstart conflicts, re-add by hand.

### Slice 5: registry row at launch/stop (DW-8; not held unless it touches credentials)
- Intent: upsert kind+endpoint row at launch, remove/mark ended at both stop paths. Files: cloud/launcher/cloud_worker.py, bin/cloud_worker.py, a fake-registry test. ~80 lines. Covers S12 (claim epic).
- Needs: slice 3 and the registry lane's `endpoint` field (#1074) landed. No old commit exists; write fresh.

## 4. Order and earliest merge (builder starts the minute batch 7 merges = day D)
- Slice 1: D (hours), no hold. Slice 2: D+1 builder done, merges when Adam clears the hold. Slice 3: D+1 to D+2 builder, merge after slice 2 and Adam's review. Slice 4: same day as slice 3 (or in it). Slice 5: after #1074, no earlier than D+1.
- Slices 2 and 3 can be built in parallel after slice 1 (they touch different hunks), but merge 2 first. The critical path is Adam's review time of 2 and 3, not build time.

## 5. Only Adam can do
- Clear the held PRs (slices 2, 3), merge-captain labels.
- Add to the App: issues write, pull requests read, checks read; accept on each installation (owner step, only in the PR body).
- Apply operator-rbac.yaml (statefulsets in apps) to the cluster.
- Decide Q1-Q3 below; after merge, authorise the live proof (S13) on the throwaway repo.

## 6. Open questions
- Q1: integration already has `--agent orchestrator` with DEFAULT_ORCHESTRATOR_SHAPE "S" and ORCHESTRATOR_ENV_NAMES: is the role value that same flag, or a new `--role`? The branch used --role; reconcile before slice 1.
- Q2: StatefulSet (the plan's pick) or bare Pod with restart Always (smaller, simplifies stop and Secret ownership).
- Q3: does the idle limit apply to an orchestrator (plan: no).
- Q4: 768Mi is unmeasured for a working orchestrator; accept a one-line row edit after the live test.
- Q5: the plan wants preflight and token-table.txt to read the table, but the old branch never touched them: confirm that rewrite is in slice 1/2 scope.
