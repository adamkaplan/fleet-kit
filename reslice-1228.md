# Re-slice of ask #1228 (retained disk): plan only, no product code

Source: origin/cw/retained-disk (PR #1230), merge-base with origin/integration c83774e2, integration is 174 commits ahead.
The branch carries two things: 21 commits of the persistent orchestrator (branch cw/orchestrator-role, ask #1068, NOT in
integration: `git merge-base --is-ancestor` says no) and 7 own commits of the disk. Own commits, in order:
aa3071b0 (core, +170/-8 launcher, +442 test, fake_kubectl, rbac +5, README), b32072ed (plain worker makes no claim call),
dba4e89e and 9a0311f8 (rename of the pod disk variable, two-part build), 6b1a64e8 (pin: operator Role has 7 resources),
45c03eb8 (credential off the disk, entrypoint + test_entrypoint_disk.py), ac7ffa0a (verify claim before attach/delete, +142 test).
Own total about +1000 lines of the 2179 (the rest is the orchestrator base). Note: a diff against the base 861c99ae shows 22 files.

## Grouping of the whole diff (+2179/-75, 22 files)
- Launcher code: cloud/launcher/cloud_worker.py (+424/-42 total; about 220 disk), cloud/launcher/app_token.py (+35/-18, orchestrator base), bin/cloud_worker.py (+15/-5, base).
- Pod spec/manifests: cloud/k8s/operator-rbac.yaml (+10; 5 disk, 5 base).
- Entrypoint: cloud/worker/entrypoint.sh (+12/-1), tests/fake_opencode.sh (+2).
- Tests: test_cloud_retained_disk.py (+442), test_cloud_retained_disk_claims.py (+142), test_entrypoint_disk.py (+102), fake_kubectl (+26/-2; 10 disk), test_cloud_orchestrator.py (+709, base), statefulset_lookup/strict (+172, base), small pin edits (k1, settings, pod_deadline, issues_read, worker).
- Docs: cloud/launcher/README.md (+40, 17 disk), cloud/README.md, docs/cloud-operator-quickstart.md (+13 each, base mostly).

## Slice 0 (prerequisite, not part of #1228): land the persistent orchestrator (cw/orchestrator-role, ask #1068)
No disk slice can start before this: the StatefulSet, role table and test_cloud_orchestrator.py exist only there.
Check status first (open PR? it is the base of #1230). Held area: the pod spec and token role. Confirm with the owner who rebases it.

## Slice A: the claim in the StatefulSet (render + resume)
- Intent: an orchestrator gets one volume claim template named after the workload, mounted at the work-tree path, size/class constants, mount never at the credential path.
- Files: cloud/launcher/cloud_worker.py, cloud/launcher/tests/fake_kubectl, cloud/launcher/tests/test_cloud_retained_disk.py (render/mount/kill subset), bin/test-cloud-launcher, cloud/launcher/tests/test_cloud_settings.py (disk variable name, see dba4e89e/9a0311f8).
- Size: about 300 changed lines. Covers: S2, S3, S4, U3, S10 (render part), U1.
- Needs: slice 0 merged. HELD (pod spec).
- Start: cherry-pick aa3071b0, dba4e89e, 9a0311f8 and b32072ed in that order (squash the two renames into aa3071b0 in the rewrite); drop the rbac, README and entrypoint hunks of aa3071b0 (they go to C, D). Conflicts expected: cloud_worker.py (8 commits on integration since base, last 293a6b83, 8eca10e8 pre-stop push #1142), fake_kubectl (2), bin/test-cloud-launcher (23 commits; chain line must be re-added by hand, see 861c99ae).

## Slice B: entrypoint keeps the credential off the disk
- Intent: the data directory stays memory-backed; only the work tree goes on the disk when present.
- Files: cloud/worker/entrypoint.sh, cloud/worker/tests/fake_opencode.sh, cloud/worker/tests/test_entrypoint_disk.py.
- Size: about 120 lines. Covers: S18, PC-3. Needs: nothing from A (the variable is read only if set), can run in parallel with A but should merge before A so the disk is never live without it. Not held (no credential path change, it removes one); still, reviewer should treat as credentials-adjacent.
- Start: cherry-pick 45c03eb8 and take the entrypoint hunks of aa3071b0/dba4e89e/9a0311f8 (3 commits touched entrypoint.sh since base on integration: re-apply by hand). Agree the variable name with A first.

## Slice C: stop keeps, delete removes, namespace guard
- Intent: stop/retire keep the claim; `--delete-disk` and `delete-disk NAME` remove it; refuse a claim in another namespace; verify a claim before attach/delete.
- Files: cloud/launcher/cloud_worker.py, bin/cloud_worker.py (CLI wiring), tests test_cloud_retained_disk.py (rest), test_cloud_retained_disk_claims.py.
- Size: about 350 lines. Covers: S6, S7, S15, S16 (mutants), U2 (call log part). Needs: A. HELD (storage lifecycle deletion).
- Start: cherry-pick ac7ffa0a, plus the stop/delete hunks of aa3071b0. bin/cloud_worker.py changed 19 times on integration: rewrite the argparse hunks, do not cherry-pick it.

## Slice D: reaper report, cost line, spend cap
- Intent: report retained disks older than RETAINED_DISK_DAYS (= 7) by name, size, age; never delete; cost line and cap count disks.
- Files: cloud/launcher/cloud_worker.py, tests (reaper, cost, cap parts of test_cloud_retained_disk.py). Size about 200 lines.
- Covers: S8, S9, S11, S12, U2. Needs: A (claims exist); independent of C. Not held (read only: claims get/list). Start: the reaper/cost hunks of aa3071b0 and b32072ed (a plain worker makes no claim call), and the "kept or Pending claim, count Mi" hunks of ac7ffa0a; rewrite onto the current cost/cap code in cloud_worker.py.

## Slice E: operator Role, docs, cluster step
- Intent: Role gets persistentvolumeclaims get,list,create,delete in cw; README states the cause, option, command, 7 days, price, dry run.
- Files: cloud/k8s/operator-rbac.yaml (+5; it has NO commits on integration since base: applies as is), cloud/launcher/README.md (8 commits since base: redo by hand), cloud/README.md, docs/cloud-operator-quickstart.md, test_cloud_k1.py pin (6b1a64e8, the Role now lists seven resources; re-check the pin count on the tip).
- Size about 120 lines. Covers: S1, S13, S17, S14 (live), S5 (live, after the apply). Needs: A (docs describe it); the Role can merge first. HELD for the owner: credentials and cluster permissions; the App permission superset is the owner's.

## What only Adam can do
1. Say who lands slice 0 and approve the held PRs (A, C, E).
2. Cluster step: review the server dry run (`kubectl apply --dry-run=server -f cloud/k8s/operator-rbac.yaml`, plus a sample claim), then give the word to apply the Role. No Azure role is needed.
3. Decide the defaults: 8 GiB, 32 GiB maximum, 7 days, 0.075 USD per GiB-month, storage class (empty means the cluster default).
4. Be present for the live check S5 (kill the real pod, see "resumed: work tree and commit present").
5. Any GitHub App permission change (none expected here).

## Order and earliest merge (if a builder starts when batch 7 merges, day 0)
Slice 0 must merge first; if it is already rebased, day 0 to day 1. B: day 0 to 1 (small). A: day 1 to 2 after 0. D and E-docs: day 2 to 3. C: day 2 to 3. E Role apply and S5: the day the owner says so, earliest day 3. Order: 0, B, A, then C and D in parallel, E (Role may go with A). Cloud pilot minimum: 0, A, B, E.

## Open questions
1. Is cw/orchestrator-role (PR before #1230) already queued for batch 7, or must it be re-sliced as well? It is the real critical path.
2. Does the ask want a different branch base than main (plan says main; assignment says integration)?
3. Should A ship the claim before the Role is applied? The launcher would fail on create until it is: order E-Role before the first launch with a disk.
4. Variable name of the disk path: keep the two-part build (the settings test pins every CW_ name)?
5. Counts above are from the commit numstat, not measured on the tip; builders must recompute after rebase.
