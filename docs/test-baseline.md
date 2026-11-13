# Test baseline (ask #2525, clause 1)

Measurement only: no test, workflow or product file is changed by this pull request. Data: `tests/baseline-times.json` (every class, every file, the 200 slowest tests, the CI job and step times).

## Headline

- **Total wall time of the whole unit-test run on ubuntu, last three real CI runs of `test.yml` on the integration branch (all three are `workflow_dispatch` runs; the newest `pull_request` run of the branch is from 6 November): 33.5, 40.6 and 23.0 minutes** (first job start to last ubuntu job end; run ids 43355343111, 43324682615, 43190655884). Average 32.4 minutes. All three runs are RED: the launcher chain step ended by its 20-minute timeout in two of them, so a green whole-run time does not exist today (see Critical path).
- **Critical path on ubuntu: the job `test-cloud (ubuntu-latest)`** (32.9, 32.7 and 20.9 minutes), dominated by the serial `bin/test-cloud-launcher` step (20.2 minutes = the timeout, 20.2, and 8.2 minutes). The slowest of the 8 switchboard shards (`shards (4/8)`: 18.8, 16.0, 11.8 minutes) and `test (ubuntu-latest)` (10.2, 10.2, 9.8 minutes) come next.
- **Compute, not wall:** summed over the 11 ubuntu jobs of run 43355343111 the jobs ran 120.8 minutes; `bin/test-switchboard` alone is 69.6 test-minutes (10903 tests, 1018 classes) in this pod, against 61.2 minutes on 4 November (8,696 tests, 712 classes).
- **Top 5 switchboard classes today (pod seconds):** TestLaunchCapacity 299 s; TestImpactHold924 203 s; TestBlockedMutants 147 s; TestGhShimEffectRule 119 s; TestRegistrarFloor 107 s.
- The macOS legs are not unit-test time on ubuntu but dominate the run: `test (macos-latest)` took 118.5, 111.4 and 103.3 minutes (whole run 119.2, 132.7, 137.8 minutes).

## 1. Total wall time and the critical path (CI)

Source: `gh api repos/adamkaplan/fleet-kit/actions/runs/<id>/jobs` (`started_at`, `completed_at`); `.github/workflows/test.yml` is identical to the integration tip (checked with `git diff` on the workflow and the chain lists: only 5 lines of `bin/test-cloud-launcher.chain` were added after the newest run, head `48c22e04`; the integration tip is `15739db1`). Job time = `completed_at - started_at`, which includes the checkout (about 30 s) but NOT the wait for a runner.

### Run 43355343111 (head `48c22e04`)

| job | minutes | result | started after run start (min) |
|---|---:|---|---:|
| test (macos-latest) | 118.5 | failure | 0.7 |
| test-cloud (macos-latest) | 40.6 | failure | 0.7 |
| test-cloud (ubuntu-latest) | 32.9 | failure | 0.7 |
| shards (4/8) | 18.8 | success | 0.7 |
| shards (6/8) | 11.3 | failure | 0.7 |
| test (ubuntu-latest) | 10.2 | success | 0.8 |
| shards (3/8) | 9.4 | success | 0.7 |
| shards (7/8) | 8.6 | success | 0.7 |
| shards (5/8) | 7.9 | success | 0.7 |
| shards (2/8) | 7.8 | success | 0.7 |
| shards (8/8) | 7.8 | success | 0.7 |
| shards (1/8) | 5.5 | success | 0.7 |
| plan | 0.6 | success | 0.0 |

Ubuntu wall (first job start to last ubuntu job end): **33.5 min**; with the macOS legs: 119.2 min. Critical path on ubuntu: `test-cloud (ubuntu-latest)`.

### Run 43324682615 (head `bb0a90f5`)

| job | minutes | result | started after run start (min) |
|---|---:|---|---:|
| test (macos-latest) | 111.4 | failure | 21.2 |
| test-cloud (macos-latest) | 41.4 | failure | 4.3 |
| test-cloud (ubuntu-latest) | 32.7 | failure | 8.0 |
| shards (4/8) | 16.0 | success | 14.4 |
| shards (6/8) | 10.8 | success | 14.2 |
| test (ubuntu-latest) | 10.2 | failure | 4.5 |
| shards (7/8) | 9.4 | success | 22.3 |
| shards (3/8) | 8.7 | success | 10.9 |
| shards (8/8) | 7.8 | success | 25.0 |
| shards (5/8) | 7.7 | success | 14.6 |
| shards (2/8) | 6.5 | success | 19.6 |
| shards (1/8) | 5.9 | success | 6.3 |
| plan | 0.6 | success | 0.0 |

Ubuntu wall (first job start to last ubuntu job end): **40.6 min**; with the macOS legs: 132.7 min. Critical path on ubuntu: `test-cloud (ubuntu-latest)`.

### Run 43190655884 (head `a8ea4d2c`)

| job | minutes | result | started after run start (min) |
|---|---:|---|---:|
| test (macos-latest) | 103.3 | failure | 34.5 |
| test-cloud (macos-latest) | 36.1 | failure | 17.2 |
| test-cloud (ubuntu-latest) | 20.9 | failure | 2.2 |
| shards (6/8) | 12.3 | success | 2.2 |
| shards (4/8) | 11.8 | success | 2.2 |
| shards (7/8) | 9.9 | success | 2.2 |
| test (ubuntu-latest) | 9.8 | failure | 2.2 |
| shards (3/8) | 9.4 | failure | 3.8 |
| shards (8/8) | 8.2 | success | 2.2 |
| shards (1/8) | 7.3 | success | 5.3 |
| shards (2/8) | 7.2 | success | 6.0 |
| shards (5/8) | 6.5 | success | 2.2 |
| plan | 2.1 | success | 0.0 |

Ubuntu wall (first job start to last ubuntu job end): **23.0 min**; with the macOS legs: 137.8 min. Critical path on ubuntu: `test-cloud (ubuntu-latest)`.

Why the runs are red (read from the logs): `bin/check-public-names` failed in runs 43324682615 and 43190655884 (a hit count; the names are not printed and are not quoted here); in run 43355343111 the launcher step hit its 20-minute timeout (`##[error]... has timed out after 20 minutes`) after 86 chain entries and the parallel entries never replayed; run 43324682615 shows the same step time (1,213 s) but its log is cut by the 5 MB log limit, so only the step time is evidence; in 43190655884 a launcher test failed and the step ended after 8.2 minutes; one shard failed in two runs (`TestArchitectureDocument` in 43355343111, `TestPrExplainer` skill size in 43190655884). The step time of a job that failed is the time to the failure.

Runner queue: in run 43324682615 the ubuntu jobs started late (no runner free): `test (ubuntu-latest)` 4.5 min, `test-cloud (ubuntu-latest)` 8.0 min, `shards (1/8)` 6.3 min, `(3/8)` 10.9, `(6/8)` 14.2, `(4/8)` 14.4, `(5/8)` 14.6, `(2/8)` 19.6, `(7/8)` 22.3 and `(8/8)` 25.0 minutes after the plan job began. Its 40.6 min wall (critical path `test-cloud (ubuntu-latest)`: 8.0 + 32.7) therefore includes 8.0 minutes of waiting that no test change can remove; the last shard to finish ended at 32.8 min. In run 43190655884 the starts were 2.2 to 6.0 minutes after the plan job.

## 2. Every suite CI runs: CI time and pod time

CI columns: seconds of the step in runs 43355343111 / 43324682615 / 43190655884 (ubuntu). Pod: this pod (6 CPU limit, 8 visible), one suite at a time, `FLEET_HEAVY=off`; the 8 switchboard shards ran 3 at a time (see What could not be measured). `rc` is the pod exit code.

| suite | CI job | CI seconds (3 runs) | pod seconds | pod rc | owner |
|---|---|---|---:|---:|---|
| bin/check-public-names | test | 7 / 8 / 6 | 0 | 0 | switchboard (bin/) |
| bin/test-heartbeat | test | 2 / skip / skip | 2 | 0 | switchboard (bin/) |
| bin/test-health-records | test | 4 / 4 / 3 | 3 | 0 | switchboard (bin/) |
| bin/test-fleet-doctor | test | 72 / 73 / 63 | 71 | 0 | switchboard (bin/) |
| bin/test-fleet-doctor-config | test | 1 / 0 / 1 | 0 | 0 | switchboard (bin/) |
| bin/test-fleet-doctor-heartbeat | test | 1 / 2 / 1 | 1 | 0 | switchboard (bin/) |
| bin/test-workflow-split | test | 1 / 1 / 1 | 1 | 0 | switchboard (bin/) |
| bin/test-path-suites | test | 115 / 116 / 103 | 111 | 0 | switchboard (bin/) |
| bin/test-gh-hold | test | 25 / 24 / 23 | 23 | 0 | switchboard (bin/) |
| bin/test-app-mint | test | 17 / 18 / 15 | 17 | 1 | switchboard (bin/) |
| bin/test-cloud-neutral-names | test | 5 / 5 / 5 | 5 | 0 | cloud lane |
| cloud/tests/test-cloud (bash, docker cases skipped) | test | 322 / 324 / 330 | 316 | 1 | cloud lane |
| node --test health.test.mjs | test | 0 / 0 / 0 | 0 | 0 | switchboard (plugins; no lane named) |
| node --test the other plugin tests | test | 0 / 0 / 0 | 0 | 0 | switchboard (plugins; no lane named) |
| bin/fleet-scenario validate | test | 2 / 2 / 1 | 1 | 0 | switchboard (bin/) |
| bin/fleet-scenario coverage | test | 1 / 1 / 2 | 1 | 0 | switchboard (bin/) |
| bin/test-cloud-worker | test-cloud | 149 / 141 / 150 | 141 | 1 | cloud lane |
| bin/test-cloud-launcher (the chain) | test-cloud | 1213 / 1213 / 490 | 2002 | 1 | cloud lane |
| bin/test-cloud-hub | test-cloud | 116 / 116 / 117 | 116 | 0 | cloud lane |
| bin/test-cloud-channel | test-cloud | 0 / 0 / 0 | 0 | 0 | cloud lane |
| bin/test-cloud-channel-server | test-cloud | 39 / 40 / 39 | 39 | 0 | cloud lane |
| bin/test-cloud-client | test-cloud | 0 / 0 / 0 | 0 | 0 | cloud lane |
| bin/test-cloud-way-back | test-cloud | 1 / 0 / 0 | 0 | 0 | cloud lane |
| bin/test-decisions-pull | test-cloud | 86 / 86 / 87 | 86 | 0 | cloud lane |
| bin/test-cloud-cli | test-cloud | 159 / 157 / 158 | 159 | 0 | cloud lane |
| bin/test-cloud-relay | test-cloud | 28 / 28 / 28 | 28 | 0 | cloud lane |
| bin/test-cloud-hub-relay | test-cloud | 35 / 37 / 37 | 35 | 0 | cloud lane |
| bin/test-cloud-relay-daemon | test-cloud | 111 / 110 / 109 | 110 | 0 | cloud lane |
| bin/test-switchboard shards 1-8 (see below) | shards (I/8) | see below | see below | - | switchboard (bin/) |

Steps that failed in CI keep the time to the failure (`check-public-names` failed in runs 2 and 3, so `test-heartbeat` shows `skip` there). The `test (ubuntu-latest)` job also runs `bin/test-switchboard` only on macOS and the nightly: it is skipped on ubuntu (the shards run it). Pod rc 1 rows: pod limits, not product failures (see What could not be measured). Rows with the launcher in the pod: see section 3.

### bin/test-switchboard shards (CI seconds in runs 43355343111 / 43324682615 / 43190655884; pod = this pod, 3 shards at a time)

| shard | CI seconds | CI minutes (mean) | pod wall s | pod tests | pod sum of test seconds | pod rc |
|---|---|---:|---:|---:|---:|---:|
| 1/8 | 292 / 319 / 403 | 5.6 | 382 | 1050 | 376 | 0 |
| 2/8 | 433 / 362 / 395 | 6.6 | 392 | 1185 | 377 | 0 |
| 3/8 | 525 / 484 / 526 | 8.5 | 485 | 2430 | 475 | 1 |
| 4/8 | 1084 / 927 / 679 | 14.9 | 982 | 1343 | 984 | 0 |
| 5/8 | 438 / 428 / 356 | 6.8 | 388 | 1266 | 384 | 0 |
| 6/8 | 642 / 621 / 703 | 10.9 | 620 | 1270 | 608 | 1 |
| 7/8 | 482 / 526 / 549 | 8.7 | 519 | 1270 | 525 | 0 |
| 8/8 | 432 / 434 / 453 | 7.3 | 456 | 1089 | 447 | 1 |
| all 8 | | 69.4 | 4224 | 10903 | 4176 | |

The pod shard failures (rc 1) are the pod, not the product: shard 3 has 20 failures all in `TestCloudReaperInstallOnMacOS` (the checkout directory `/work/repo` has a group-writable parent, which the reaper install refuses); shards 6 and 8 have 37 and 58 failing or erroring test lines, the first named `test_the_service_file_is_private_in_every_case` (file-mode and ownership checks of the service install code, which this pod's filesystem fails: the same family); timings of a failing test are still real. The shard split is today's `tests/switchboard/class_times.json` (stale: it is the 4 November table, 713 classes, 3,673 s; today there are 1018 classes), which is why the shards are unbalanced: shard 3 holds 2,430 tests and shard 4 takes 982 s of pod wall against a median of 470 s.
## 3. Time of each class and each file

All 1018 classes and 172 files of `bin/test-switchboard` (including the classes loaded from `tests/switchboard/`) are in `tests/baseline-times.json` (`switchboard_classes`, `switchboard_files`, seconds measured in the pod with the sum of per-test times from a `unittest` result subclass; CI does not print per-test times, so per-class CI times cannot be read from a run: only the step total per shard is in section 2). Here: the files and classes of at least 5 s, then the slowest 40 classes.

| file | tests | seconds | share | owner |
|---|---:|---:|---:|---|
| bin/test-switchboard | 6192 | 1653.9 | 39.6% | switchboard (bin/) |
| tests/switchboard/test_launch_capacity.py | 47 | 299.1 | 7.2% | switchboard (bin/) |
| tests/switchboard/test_gh_shim_sign_input.py | 131 | 259.6 | 6.2% | switchboard (bin/) |
| tests/switchboard/test_924.py | 64 | 212.5 | 5.1% | switchboard (bin/) |
| tests/switchboard/test_stop_final_message.py | 79 | 165.1 | 4.0% | switchboard (bin/) |
| tests/switchboard/test_new_test_checks.py | 23 | 110.5 | 2.6% | switchboard (bin/) |
| tests/switchboard/test_host_disk.py | 10 | 105.7 | 2.5% | switchboard (bin/) |
| tests/switchboard/test_cases_10.py | 117 | 90.7 | 2.2% | switchboard (bin/) |
| tests/switchboard/test_plan_hash_marker.py | 63 | 87.9 | 2.1% | switchboard (bin/) |
| tests/switchboard/test_cases_07.py | 142 | 73.2 | 1.8% | switchboard (bin/) |
| tests/switchboard/test_queued_behind_turn.py | 19 | 55.2 | 1.3% | switchboard (bin/) |
| tests/switchboard/test_cases_12.py | 118 | 48.3 | 1.2% | switchboard (bin/) |
| tests/switchboard/test_launched_reviewers.py | 16 | 43.8 | 1.0% | switchboard (bin/) |
| tests/switchboard/test_heavy_progress.py | 25 | 42.4 | 1.0% | switchboard (bin/) |
| tests/switchboard/test_wired_tests.py | 30 | 42.2 | 1.0% | switchboard (bin/) |
| tests/switchboard/test_cases_04.py | 192 | 39.9 | 1.0% | switchboard (bin/) |
| tests/switchboard/test_closure.py | 16 | 39.8 | 1.0% | switchboard (bin/) |
| tests/switchboard/test_cases_03.py | 180 | 38.9 | 0.9% | switchboard (bin/) |
| tests/switchboard/test_gh_shim_body.py | 42 | 35.2 | 0.8% | switchboard (bin/) |
| tests/switchboard/test_cases_01.py | 177 | 26.6 | 0.6% | switchboard (bin/) |
| tests/switchboard/test_cases_11.py | 150 | 26.4 | 0.6% | switchboard (bin/) |
| tests/switchboard/test_header_cache.py | 43 | 25.3 | 0.6% | switchboard (bin/) |
| tests/switchboard/test_guard_set.py | 11 | 24.0 | 0.6% | switchboard (bin/) |
| tests/switchboard/test_architecture.py | 30 | 22.6 | 0.5% | switchboard (bin/) |
| tests/switchboard/test_shim_guards.py | 13 | 21.8 | 0.5% | switchboard (bin/) |
| tests/test_installed_guard.py | 54 | 21.4 | 0.5% | switchboard (bin/) |
| tests/switchboard/test_batch_carry.py | 27 | 19.4 | 0.5% | switchboard (bin/) |
| tests/switchboard/test_cases_08.py | 148 | 19.0 | 0.5% | switchboard (bin/) |
| tests/switchboard/test_end_on_ask.py | 40 | 18.1 | 0.4% | switchboard (bin/) |
| tests/switchboard/test_mac_legs.py | 30 | 17.9 | 0.4% | switchboard (bin/) |
| tests/switchboard/test_size_ceilings.py | 28 | 17.2 | 0.4% | switchboard (bin/) |
| tests/switchboard/test_plan_failure_mode.py | 10 | 15.6 | 0.4% | switchboard (bin/) |
| tests/switchboard/test_held_sends.py | 32 | 15.4 | 0.4% | switchboard (bin/) |
| tests/switchboard/test_ci_outside_bin.py | 8 | 15.2 | 0.4% | switchboard (bin/) |
| tests/switchboard/test_sent_keys_age.py | 9 | 14.8 | 0.4% | switchboard (bin/) |
| tests/switchboard/test_land_gate.py | 26 | 13.5 | 0.3% | switchboard (bin/) |
| tests/switchboard/test_gh_shim_sign.py | 18 | 12.9 | 0.3% | switchboard (bin/) |
| tests/switchboard/test_ci_record_proof.py | 6 | 12.6 | 0.3% | switchboard (bin/) |
| tests/switchboard/test_pins.py | 39 | 12.1 | 0.3% | switchboard (bin/) |
| tests/switchboard/test_cases_02.py | 193 | 12.0 | 0.3% | switchboard (bin/) |
| tests/switchboard/test_new_module_demands.py | 6 | 11.8 | 0.3% | switchboard (bin/) |
| tests/switchboard/test_unconsumed_steer.py | 39 | 11.7 | 0.3% | switchboard (bin/) |
| tests/switchboard/test_triage_parked_digest.py | 10 | 11.2 | 0.3% | switchboard (bin/) |
| tests/switchboard/test_land_repos.py | 26 | 10.9 | 0.3% | switchboard (bin/) |
| tests/switchboard/test_plan_rehearsal.py | 16 | 9.6 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_relay_loop_cadence.py | 26 | 9.4 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_cases_06.py | 52 | 9.3 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_gate_probe_reads.py | 3 | 9.1 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_heavy_holders.py | 10 | 9.1 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_gh_shim_sign_repo_flag.py | 9 | 8.7 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_open_list_cuts.py | 17 | 8.7 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_comment_cursor.py | 33 | 8.3 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_pod_niceness.py | 7 | 8.3 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_1241.py | 13 | 8.1 | 0.2% | switchboard (bin/) |
| tests/test_doctor_watcher.py | 4 | 8.0 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_github_write_audit.py | 13 | 7.8 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_cases_05.py | 234 | 7.8 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_ci_test_files.py | 8 | 7.3 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_launch_model.py | 25 | 7.2 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_lost_worker.py | 64 | 7.2 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_landed_subject.py | 21 | 7.1 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_claim_legacy.py | 11 | 6.9 | 0.2% | switchboard (bin/) |
| tests/switchboard/test_gh_shim_hook.py | 22 | 5.9 | 0.1% | switchboard (bin/) |
| tests/switchboard/test_claims_note.py | 27 | 5.9 | 0.1% | switchboard (bin/) |
| tests/switchboard/test_body_check.py | 19 | 5.3 | 0.1% | switchboard (bin/) |
| tests/switchboard/test_changed_files_whole.py | 20 | 5.3 | 0.1% | switchboard (bin/) |

Total of all 172 files: 4176.5 s (69.6 minutes), 10903 tests. Files under 5 s: 106, together 133.2 s.

`bin/test-switchboard` itself holds 6192 of the 10903 tests (file `bin/test-switchboard`, 80,493 lines) and `tests/switchboard/` the rest; the `shard` of a class is the shard of the last of its tests seen (a class name defined in more than one file can span shards, so per-class shard counts differ slightly from the shard table); `builtins`-module classes are classes built inside a function (`TestImpactHold924`, `TestLaunchCapacity` and others) and are placed by the file that defines them.

| class | file | tests | seconds | shard | owner |
|---|---|---:|---:|---:|---|
| TestLaunchCapacity | tests/switchboard/test_launch_capacity.py | 47 | 299.1 | 4 | switchboard (bin/) |
| TestImpactHold924 | tests/switchboard/test_924.py | 61 | 203.1 | 6 | switchboard (bin/) |
| TestBlockedMutants | bin/test-switchboard | 4 | 146.9 | 4 | switchboard (bin/) |
| TestGhShimEffectRule | tests/switchboard/test_gh_shim_sign_input.py | 12 | 119.2 | 4 | switchboard (bin/) |
| TestRegistrarFloor | bin/test-switchboard | 45 | 106.8 | 1 | switchboard (bin/) |
| TestHostDisk | tests/switchboard/test_host_disk.py | 7 | 105.7 | 4 | switchboard (bin/) |
| TestFactsReadAtRunTime | bin/test-switchboard | 2 | 97.5 | 2 | switchboard (bin/) |
| TestOfflineScenarios | bin/test-switchboard | 76 | 91.1 | 7 | switchboard (bin/) |
| TestNewTestChecksMutants | tests/switchboard/test_new_test_checks.py | 6 | 88.4 | 8 | switchboard (bin/) |
| TestGhShimSignInput | tests/switchboard/test_gh_shim_sign_input.py | 38 | 85.3 | 1 | switchboard (bin/) |
| TestStopFinalMessageBareMutant | tests/switchboard/test_stop_final_message.py | 2 | 65.8 | 7 | switchboard (bin/) |
| TestImpactHold | bin/test-switchboard | 120 | 65.8 | 7 | switchboard (bin/) |
| TestPlanHashMarkerLaunch | tests/switchboard/test_plan_hash_marker.py | 5 | 65.6 | 3 | switchboard (bin/) |
| TestStopFinalMessageBare | tests/switchboard/test_stop_final_message.py | 6 | 65.2 | 3 | switchboard (bin/) |
| TestArchitectureSafetyNet | tests/switchboard/test_cases_10.py | 17 | 62.9 | 5 | switchboard (bin/) |
| TestPlanReviewCommands | bin/test-switchboard | 53 | 59.8 | 6 | switchboard (bin/) |
| TestQueuedBehindTurn | tests/switchboard/test_queued_behind_turn.py | 19 | 55.2 | 6 | switchboard (bin/) |
| TestResume | bin/test-switchboard | 27 | 45.1 | 7 | switchboard (bin/) |
| TestHeavyProgress | tests/switchboard/test_heavy_progress.py | 25 | 42.4 | 8 | switchboard (bin/) |
| TestShimTopSpender | bin/test-switchboard | 15 | 41.6 | 8 | switchboard (bin/) |
| TestIdentityCheck | bin/test-switchboard | 41 | 37.5 | 4 | switchboard (bin/) |
| TestLaunchedReviewerMutants | tests/switchboard/test_launched_reviewers.py | 1 | 36.6 | 7 | switchboard (bin/) |
| TestGhShimRawPath | tests/switchboard/test_gh_shim_sign_input.py | 12 | 33.1 | 2 | switchboard (bin/) |
| TestGhShimMergeGate | bin/test-switchboard | 46 | 31.8 | 5 | switchboard (bin/) |
| TestHeavySlots | tests/switchboard/test_cases_07.py | 28 | 30.1 | 3 | switchboard (bin/) |
| TestRecordingCommands | bin/test-switchboard | 23 | 29.9 | 2 | switchboard (bin/) |
| TestMadeUpPids | bin/test-switchboard | 17 | 29.5 | 4 | switchboard (bin/) |
| TestDerivedClosure | tests/switchboard/test_closure.py | 8 | 27.7 | 2 | switchboard (bin/) |
| TestReplay | tests/switchboard/test_header_cache.py | 4 | 24.6 | 3 | switchboard (bin/) |
| TestPlanLedger | bin/test-switchboard | 54 | 24.1 | 7 | switchboard (bin/) |
| TestMutantIsCaught | tests/switchboard/test_wired_tests.py | 5 | 23.4 | 6 | switchboard (bin/) |
| TestSupportRebinding | bin/test-switchboard | 6 | 22.8 | 5 | switchboard (bin/) |
| TestGhBodyMutants | tests/switchboard/test_gh_shim_body.py | 1 | 22.4 | 5 | switchboard (bin/) |
| TestGuardSet | tests/switchboard/test_guard_set.py | 7 | 22.2 | 5 | switchboard (bin/) |
| TestOwnMarkerNotHonoured | tests/switchboard/test_cases_10.py | 15 | 21.6 | 3 | switchboard (bin/) |
| TestPlanHashMarkerRounds | tests/switchboard/test_plan_hash_marker.py | 21 | 20.9 | 3 | switchboard (bin/) |
| TestArchitectureDocument | tests/switchboard/test_architecture.py | 5 | 20.8 | 6 | switchboard (bin/) |
| TestLaunch | bin/test-switchboard | 157 | 20.6 | 3 | switchboard (bin/) |
| TestScenarioFiles | tests/switchboard/test_cases_04.py | 8 | 20.1 | 6 | switchboard (bin/) |
| TestShimGuardsTool | tests/switchboard/test_shim_guards.py | 4 | 19.7 | 4 | switchboard (bin/) |

### Cloud launcher chain (`bin/test-cloud-launcher.chain`, 193 entries; 32 on the parallel list)

Chain TIME lines (whole seconds, measured in the pod by `bin/test-cloud-launcher` itself; entries on the parallel list print their TIME inside their lane's output, replayed at the end). CI TIME lines exist only for the serial entries that ran before the 20-minute timeout (run 43355343111: 86 TIME lines, sum 1,205 s; run 43190655884: 53 TIME lines, sum 483 s, the step ended at 490 s on a test failure).

| entry | pod s | CI s (run 43355343111) | owner |
|---|---:|---:|---|
| cloud/launcher/tests/test_no_open_files.py | 308 | 384 | cloud lane |
| cloud/launcher/tests/test_git_token_renewal.py | 198 | - | cloud lane |
| cloud/launcher/tests/test_run_record.py | 161 | - | cloud lane |
| cloud/launcher/tests/test_preflight_repo_read.py | 116 | 120 | cloud lane |
| cloud/launcher/tests/test_mint_roles.py | 110 | - | cloud lane |
| cloud/launcher/tests/test_scrub_linear.py | 96 | - | cloud lane |
| cloud/launcher/tests/test_driver.py | 95 | - | cloud lane |
| cloud/worker/tests/test_cloud_prepush_background.py | 92 | - | cloud lane |
| cloud/launcher/tests/test_registry_endpoint.py | 89 | - | cloud lane |
| cloud/launcher/tests/test_refusal_label.py | 88 | - | cloud lane |
| cloud/launcher/tests/test_persistent_orchestrator.py | 86 | 86 | cloud lane |
| cloud/launcher/tests/test_pdb.py | 82 | - | cloud lane |
| cloud/launcher/tests/test_cloud_stack_wait.py | 63 | - | cloud lane |
| cloud/launcher/tests/test_brief_header.py | 58 | 66 | cloud lane |
| cloud/launcher/tests/test_cloud_quick_scans_run.py | 54 | - | cloud lane |
| cloud/launcher/tests/test_stub_cleanup.py | 50 | - | cloud lane |
| cloud/launcher/tests/test_cloud_k1.py | 45 | - | cloud lane |
| cloud/launcher/tests/test_scratch_clean.py | 43 | - | cloud lane |
| cloud/launcher/tests/test_cloud_stack.py | 43 | - | cloud lane |
| cloud/launcher/tests/test_kept_expiry.py | 42 | 41 | cloud lane |
| cloud/launcher/tests/test_cloud_worker.py | 41 | - | cloud lane |
| cloud/launcher/tests/test_failed_pod_record.py | 38 | 36 | cloud lane |
| cloud/launcher/tests/test_stall_structural.py | 38 | - | cloud lane |
| cloud/launcher/tests/test_app_token.py | 38 | - | cloud lane |
| cloud/launcher/tests/test_reaper_managed.py | 35 | - | cloud lane |
| cloud/worker/tests/test_plan_check_in_pod.py | 35 | - | cloud lane |
| cloud/launcher/tests/test_stop_no_checkout.py | 32 | - | cloud lane |
| cloud/launcher/tests/test_persistent_merge_fix.py | 29 | 32 | cloud lane |
| cloud/launcher/tests/test_stop_cause.py | 29 | - | cloud lane |
| cloud/launcher/tests/test_preflight.py | 28 | - | cloud lane |
| cloud/launcher/tests/test_cloud_prestop.py | 27 | - | cloud lane |
| bin/test-cloud-retired | 26 | 33 | cloud lane |
| cloud/launcher/tests/test_vault_block.py | 25 | - | cloud lane |
| cloud/launcher/tests/test_worker_copies.py | 25 | - | cloud lane |
| cloud/worker/tests/test_cloud_prepush_hardening.py | 25 | - | cloud lane |
| cloud/launcher/tests/test_cloud_quick_bounds.py | 24 | - | cloud lane |
| cloud/launcher/tests/test_reap_concurrent.py | 23 | - | cloud lane |
| cloud/launcher/tests/test_m_cap.py | 23 | - | cloud lane |
| cloud/launcher/tests/test_fixture_root.py | 22 | 36 | cloud lane |
| cloud/launcher/tests/test_orchestrator_agent.py | 22 | 21 | cloud lane |
| cloud/launcher/tests/test_retire_gone_exact.py | 21 | - | cloud lane |
| cloud/launcher/tests/test_cloud_settings.py | 20 | 34 | cloud lane |
| cloud/launcher/tests/test_cloud_status_summary.py | 19 | 22 | cloud lane |
| cloud/launcher/tests/test_proof_operator_kubectl.py | 19 | - | cloud lane |
| cloud/worker/tests/test_cw_run.py | 19 | - | cloud lane |
| cloud/launcher/tests/test_cloud_model_served.py | 18 | 23 | cloud lane |
| cloud/launcher/tests/test_untracked_report.py | 15 | - | cloud lane |
| cloud/worker/tests/test_cloud_prepush_preflight.py | 15 | - | cloud lane |
| cloud/launcher/tests/test_cluster_token_refresh.py | 14 | 15 | cloud lane |
| cloud/worker/tests/test_cloud_prepush.py | 14 | - | cloud lane |
| cloud/launcher/tests/test_app_token_route.py | 13 | 17 | cloud lane |
| cloud/launcher/tests/test_idle_default_note.py | 13 | 13 | cloud lane |
| cloud/launcher/tests/test_known_forms.py | 13 | 16 | cloud lane |
| cloud/launcher/tests/test_cloud_tests_write_tmp.py | 12 | 20 | cloud lane |
| cloud/launcher/tests/test_launch_idempotent.py | 12 | 14 | cloud lane |
| cloud/launcher/tests/test_preflight_step.py | 12 | 12 | cloud lane |
| cloud/launcher/tests/test_token_at_limit.py | 12 | - | cloud lane |
| cloud/launcher/tests/test_kept_failed_once.py | 11 | 11 | cloud lane |
| cloud/worker/tests/test_cw_run_wake.py | 11 | - | cloud lane |
| cloud/launcher/tests/test_issues_read.py | 11 | - | cloud lane |
| cloud/launcher/tests/test_results_own_run.py | 10 | - | cloud lane |
| cloud/launcher/tests/test_cloud_shapes.py | 9 | 12 | cloud lane |
| cloud/launcher/tests/test_hub_entry_keys.py | 9 | 9 | cloud lane |
| cloud/launcher/tests/test_needs_docker_hermetic.py | 9 | 9 | cloud lane |
| cloud/launcher/tests/test_cloud_pr_preflight.py | 8 | 12 | cloud lane |
| cloud/launcher/tests/test_image_proof_launch.py | 8 | 9 | cloud lane |
| cloud/launcher/tests/test_repo_min_shape.py | 8 | - | cloud lane |
| cloud/worker/tests/test_cw_gh.py | 8 | - | cloud lane |
| cloud/launcher/tests/test_orchestrator_hub_session.py | 8 | - | cloud lane |
| cloud/launcher/tests/test_cloud_split.py | 8 | - | cloud lane |
| cloud/launcher/tests/test_m_cap_stopped.py | 7 | 8 | cloud lane |
| cloud/launcher/tests/test_retire_gone_pod.py | 7 | - | cloud lane |
| cloud/worker/tests/test_activity.py | 7 | - | cloud lane |
| cloud/launcher/tests/test_token_service.py | 6 | - | cloud lane |
| cloud/launcher/tests/test_cloud_default_model.py | 5 | 4 | cloud lane |
| cloud/launcher/tests/test_cloud_preflight_quick.py | 5 | 7 | cloud lane |
| cloud/launcher/tests/test_cloud_preflight_wired.py | 5 | 6 | cloud lane |
| cloud/launcher/tests/test_copilot_front.py | 5 | 7 | cloud lane |
| cloud/launcher/tests/test_orchestrator_row.py | 5 | 4 | cloud lane |
| cloud/launcher/tests/test_results_own_run_wiring.py | 5 | - | cloud lane |
| cloud/launcher/tests/test_stack_model_key.py | 5 | - | cloud lane |
| cloud/launcher/tests/test_stop_failed_pod.py | 5 | - | cloud lane |
| cloud/launcher/tests/test_wip_save.py | 5 | - | cloud lane |
| cloud/launcher/tests/test_app_token_service.py | 4 | 6 | cloud lane |
| cloud/launcher/tests/test_create_says_why.py | 4 | 3 | cloud lane |
| cloud/launcher/tests/test_cw_names.py | 4 | 2 | cloud lane |
| cloud/launcher/tests/test_image_gate.py | 4 | 3 | cloud lane |
| cloud/launcher/tests/test_kept_running_once.py | 4 | 5 | cloud lane |
| cloud/launcher/tests/test_nudge_reused.py | 4 | 4 | cloud lane |
| cloud/launcher/tests/test_reaper_audit_tail.py | 4 | - | cloud lane |
| cloud/worker/tests/test_activity_message.py | 4 | - | cloud lane |
| cloud/launcher/tests/test_cloud_inspect.py | 3 | 4 | cloud lane |
| cloud/launcher/tests/test_end_hook_run_files.py | 3 | 4 | cloud lane |
| cloud/worker/tests/test_pod_notes_log.py | 3 | - | cloud lane |
| cloud/worker/tests/test_scan_dpkg_skip.py | 3 | - | cloud lane |
| cloud/launcher/tests/test_git_token_retry.py | 3 | - | cloud lane |
| cloud/launcher/tests/test_cloud_pod_replaced.py | 2 | 2 | cloud lane |
| cloud/launcher/tests/test_end_hook_nothing_to_push.py | 2 | 2 | cloud lane |
| cloud/launcher/tests/test_idle_default_60.py | 2 | 3 | cloud lane |
| cloud/launcher/tests/test_l_hard_default.py | 2 | 2 | cloud lane |
| cloud/launcher/tests/test_pod_state.py | 2 | 2 | cloud lane |
| cloud/launcher/tests/test_read_retired_last_message.py | 2 | 2 | cloud lane |
| cloud/launcher/tests/test_chain_list.py | 1 | 1 | cloud lane |
| cloud/launcher/tests/test_cloud_allowlist.py | 1 | 1 | cloud lane |
| cloud/launcher/tests/test_cloud_quick_guards.py | 1 | 2 | cloud lane |
| cloud/launcher/tests/test_cloud_quick_selector.py | 1 | 1 | cloud lane |
| cloud/launcher/tests/test_cloud_turn_end.py | 1 | 2 | cloud lane |
| cloud/launcher/tests/test_final_note.py | 1 | 2 | cloud lane |
| cloud/launcher/tests/test_land_report.py | 1 | 1 | cloud lane |
| cloud/launcher/tests/test_land_shape.py | 1 | 0 | cloud lane |
| cloud/launcher/tests/test_m_request.py | 1 | 0 | cloud lane |
| cloud/launcher/tests/test_model_credential.py | 1 | 1 | cloud lane |
| cloud/launcher/tests/test_no_commit_mark.py | 1 | 1 | cloud lane |
| cloud/launcher/tests/test_pr_label_reaper.py | 1 | 1 | cloud lane |
| cloud/launcher/tests/test_probe_redirect.py | 1 | 1 | cloud lane |
| bin/test-cloud-cost-report | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_all_reds.py | 0 | 1 | cloud lane |
| cloud/launcher/tests/test_azure_config_dir.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_chain_time.py | 0 | 1 | cloud lane |
| cloud/launcher/tests/test_cloud_docs_wording.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_cloud_end_alert.py | 0 | 1 | cloud lane |
| cloud/launcher/tests/test_cloud_hub_manifest.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_cloud_hub_network.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_cloud_quick_budget.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_copilot_manifest.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_council_url_render.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_cw_minter_manifest.py | 0 | 1 | cloud lane |
| cloud/launcher/tests/test_docs_preflight_modes.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_end_hook_pr_ready.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_hub_restart_client.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_hub_safe_to_evict.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_idle_nudge.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_idle_nudge_skipped.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_image_roll.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_load_part.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_long_run_setting.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_mint_retry.py | 0 | 1 | cloud lane |
| cloud/launcher/tests/test_orphan_secrets.py | 0 | 1 | cloud lane |
| cloud/launcher/tests/test_pod_boundaries.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_pod_requests.py | 0 | 0 | cloud lane |
| cloud/launcher/tests/test_readonly_root.py | 0 | 0 | cloud lane |

51 further entries took under 3 s each in the pod (sum 33 s); all 192 entries are in the JSON (`launcher_files_pod`). Sum of the TIME lines: 3060 s (51.0 min of work; the chain's wall time with its 3 parallel lanes was 2,002 s = 33.4 min); entries with a TIME line: 192 of 193.

### Cloud worker suite (`bin/test-cloud-worker`: 152 tests, 140 s in the pod, 141-150 s in CI)

| class | tests | seconds | owner |
|---|---:|---:|---|
| TestWiring | 27 | 24.9 | cloud lane |
| TestLaunchStack | 14 | 20.1 | cloud lane |
| TestSignalsAndOrphans | 4 | 12.6 | cloud lane |
| TestVaultCheckProcess | 7 | 12.0 | cloud lane |
| TestSettingsFile | 15 | 10.5 | cloud lane |
| TestVaultCheckRelay | 13 | 9.0 | cloud lane |
| TestKeptRetireNamesTheSave | 7 | 8.1 | cloud lane |
| TestCloudWorkerEndpoint | 11 | 6.6 | cloud lane |
| TestNeedsDocker | 5 | 5.2 | cloud lane |
| TestLiveFindings | 6 | 5.0 | cloud lane |
| TestVaultCheckPath | 7 | 4.8 | cloud lane |
| TestStopAndStatus | 8 | 4.6 | cloud lane |
| TestLauncherLookup | 6 | 3.3 | cloud lane |
| TestLocalLaunchIsNeverBrokenByTheCloudModule | 5 | 3.1 | cloud lane |
| TestLaunchRunsThePreflight | 5 | 2.9 | cloud lane |
| TestCloudWorkerLifecycle | 3 | 2.8 | cloud lane |
| TestLauncherLookupInputs | 5 | 2.5 | cloud lane |
| TestNameCollision | 3 | 2.0 | cloud lane |
| TestSameAllowList | 1 | 0.0 | cloud lane |

The other python suites have per-class times in the JSON (`other_suites`).

## 4. The slowest 50 (classes, chain files and whole suites that cannot be split)

Seconds are pod seconds. Tag vocabulary: `sleeps/real waits`, `real subprocess/server`, `serial chain`, `big fixture/setup`, `parse-once candidate`, `mutant runs`. Cause lines for rows 1-30 come from reading the class's code and its slowest tests (the slowest tests are listed in the second table); rows 31-50 are tagged from the code too, a shorter read. Launcher chain files (names ending `.py`) have no per-test times, so their cause lines come from the file's header and counts of sleep, subprocess and mutant mentions, not from a test-by-test read: treat those as leads for the cloud lane.

| # | name | file | seconds | tags | cause | owner |
|---:|---|---|---:|---|---|---|
| 1 | cloud/tests/test-cloud (the whole bash suite: 183+ ok lines, not split) | cloud/tests/test-cloud | 315.9 | sleeps/real waits; real subprocess/server | one bash script: real child processes (stub docker, fake containerd, python archive builders) with `sleep 0.1` poll loops and `sleep 3` stubs; not split into tests, so it is one row. | cloud lane |
| 2 | test_no_open_files.py | cloud/launcher/tests/test_no_open_files.py | 308.0 | real subprocess/server; serial chain | re-runs every launcher test file in its own subprocess under `-W error::ResourceWarning` (4 at a time), so the chain's work is paid twice; it also runs serially in the chain (a parallel-list candidate). | cloud lane |
| 3 | TestLaunchCapacity | tests/switchboard/test_launch_capacity.py | 299.1 | mutant runs; real subprocess/server | test_u23 mutant cases copy the cloud_worker source and run the 400-line PC-11 battery as a child process per case (160 s + 79 s in two tests). | switchboard (bin/) |
| 4 | TestImpactHold924 | tests/switchboard/test_924.py | 203.1 | parse-once candidate; mutant runs | 61 tests, each loops over 100+ daemon/helper functions, patching one function at a time and re-driving the whole clear (`reassign_registrar` per function). | switchboard (bin/) |
| 5 | test_git_token_renewal.py | cloud/launcher/tests/test_git_token_renewal.py | 198.0 | mutant runs; big fixture/setup | 27 mutant mentions: scratch-copy mutants of the reaper, each run through the fake kubectl (198 s in the pod; parallel list). | cloud lane |
| 6 | test_run_record.py | cloud/launcher/tests/test_run_record.py | 161.0 | real subprocess/server; big fixture/setup | 4 subprocess uses plus the stub server and fake kubectl (161 s; parallel list). | cloud lane |
| 7 | TestBlockedMutants | bin/test-switchboard | 146.9 | mutant runs | negates every guard of the blocked report in a source copy and runs the named killer test for each (103 s + 41 s in two tests). | switchboard (bin/) |
| 8 | TestGhShimEffectRule | tests/switchboard/test_gh_shim_sign_input.py | 119.2 | mutant runs; real subprocess/server | each `mutant4` test rewrites the gh shim source and runs the whole R4 spelling battery through a real subprocess; about 17 s each, 5 mutants + control. | switchboard (bin/) |
| 9 | test_preflight_repo_read.py | cloud/launcher/tests/test_preflight_repo_read.py | 116.0 | real subprocess/server | starts its own loopback fake GitHub and runs the real preflight.py as a subprocess for every case; no sleeps. | cloud lane |
| 10 | test_mint_roles.py | cloud/launcher/tests/test_mint_roles.py | 110.0 | mutant runs; real subprocess/server | 9 mutant mentions; the real mint runs as a subprocess per role (110 s). | cloud lane |
| 11 | TestRegistrarFloor | bin/test-switchboard | 106.8 | parse-once candidate; mutant runs | re-derives the daemon closure and registrar lists, then runs the lint on every mutant (29 s in one test) over a 1,330-line class; many cases re-parse the entry. | switchboard (bin/) |
| 12 | TestHostDisk | tests/switchboard/test_host_disk.py | 105.7 | real subprocess/server; big fixture/setup | one test re-runs a named group of classes in one process with the disk faked at 5 GB (100 s): a second full execution of other classes. | switchboard (bin/) |
| 13 | TestFactsReadAtRunTime | bin/test-switchboard | 97.5 | mutant runs; big fixture/setup | 2 tests: every fact name x every spelling re-drives the gate path with a recording proxy (96 s in one test). | switchboard (bin/) |
| 14 | test_scrub_linear.py | cloud/launcher/tests/test_scrub_linear.py | 96.0 | mutant runs | 43 mutant mentions: each check against the frozen old scrub, many generated inputs (96 s). | cloud lane |
| 15 | test_driver.py | cloud/launcher/tests/test_driver.py | 95.0 | sleeps/real waits; real subprocess/server | 8 sleep calls and 7 subprocess uses: the operator driver run_task.py is started and waited on. | cloud lane |
| 16 | test_cloud_prepush_background.py | cloud/worker/tests/test_cloud_prepush_background.py | 92.0 | sleeps/real waits; real subprocess/server | the background prepush runs real child processes and is polled. | cloud lane |
| 17 | TestOfflineScenarios | bin/test-switchboard | 91.1 | big fixture/setup | one test per scenario file; q1 (two fake hours, 26 s) and g3 (14 s) drive thousands of fake-clock passes; no real waits. | switchboard (bin/) |
| 18 | test_registry_endpoint.py | cloud/launcher/tests/test_registry_endpoint.py | 89.0 | mutant runs; big fixture/setup | mutants copy the source tree to a scratch directory and run the behaviour tests in it; the launch runs on the fake cluster. | cloud lane |
| 19 | TestNewTestChecksMutants | tests/switchboard/test_new_test_checks.py | 88.4 | mutant runs; real subprocess/server | six mutants, each on a source copy, each run as a real check subprocess (about 14-17 s each). | switchboard (bin/) |
| 20 | test_refusal_label.py | cloud/launcher/tests/test_refusal_label.py | 88.0 | real subprocess/server | loopback stub of GitHub plus 8 refusal labels each through the launcher (subprocess per launch). | cloud lane |
| 21 | test_persistent_orchestrator.py | cloud/launcher/tests/test_persistent_orchestrator.py | 86.0 | mutant runs; big fixture/setup | 1,200 lines, 37 mutant mentions: scratch-copy mutants and the fake-kubectl cluster per scenario. | cloud lane |
| 22 | TestGhShimSignInput | tests/switchboard/test_gh_shim_sign_input.py | 85.3 | real subprocess/server; big fixture/setup | 38 tests run the real shim as a subprocess through a rig (each spelling a process). | switchboard (bin/) |
| 23 | test_pdb.py | cloud/launcher/tests/test_pdb.py | 82.0 | mutant runs; real subprocess/server | 22 mutant mentions: scratch copies of the launcher, each run through the fake kubectl. | cloud lane |
| 24 | Mutants | bin/test-decisions-pull | 81.0 | mutant runs | 82 mutants of the decisions-pull source, each `load()` re-executes the module (about 1 s each). | cloud lane |
| 25 | TestRefusals | bin/test-cloud-cli | 67.5 | sleeps/real waits | a local HTTP server with a stalled or cut body: tests wait out real socket timeouts (`error_body_cut_mid_read` 31.7 s). | cloud lane |
| 26 | TestStopFinalMessageBareMutant | tests/switchboard/test_stop_final_message.py | 65.8 | mutant runs; real subprocess/server | copies the check file and tests to a scratch tree and runs it as a child (58.6 s in one test). | switchboard (bin/) |
| 27 | TestImpactHold | bin/test-switchboard | 65.8 | parse-once candidate; big fixture/setup | 120 tests on one 2,358-line class, each building a repository fixture world; 29 parse/read uses. | switchboard (bin/) |
| 28 | TestPlanHashMarkerLaunch | tests/switchboard/test_plan_hash_marker.py | 65.6 | mutant runs; real subprocess/server | control and mutant each run the real `launch` on a copy of the entry (33 s each). | switchboard (bin/) |
| 29 | TestStopFinalMessageBare | tests/switchboard/test_stop_final_message.py | 65.2 | real subprocess/server | U20 runs the whole check file again as a nested subprocess (54 s). | switchboard (bin/) |
| 30 | test_cloud_stack_wait.py | cloud/launcher/tests/test_cloud_stack_wait.py | 63.0 | sleeps/real waits | 23 sleep mentions; the file names a fake clock, so most are the fake pause; its time is in the retry-attempt loops (63 s). | cloud lane |
| 31 | TestArchitectureSafetyNet | tests/switchboard/test_cases_10.py | 62.9 | real subprocess/server; big fixture/setup | records every verb of the entry with a process pool and again to prove it deterministic (60 s in one test). | switchboard (bin/) |
| 32 | TestPlanReviewCommands | bin/test-switchboard | 59.8 | big fixture/setup | 53 tests at 0.5-3.6 s: each builds the plan-review world with the real engine. | switchboard (bin/) |
| 33 | test_brief_header.py | cloud/launcher/tests/test_brief_header.py | 58.0 | mutant runs; real subprocess/server | 1,012 lines, 38 mutant mentions, 6 subprocess uses: scratch-copy mutants of the launcher. | cloud lane |
| 34 | TestQueuedBehindTurn | tests/switchboard/test_queued_behind_turn.py | 55.2 | mutant runs; real subprocess/server | s5 copies the tree twice and runs two tests in each (35 s); the light-switch test is 17 s. | switchboard (bin/) |
| 35 | test_cloud_quick_scans_run.py | cloud/launcher/tests/test_cloud_quick_scans_run.py | 54.0 | real subprocess/server | runs every named quick scan for real on a clean tree, 8 subprocess uses (54 s). | cloud lane |
| 36 | test_stub_cleanup.py | cloud/launcher/tests/test_stub_cleanup.py | 50.0 | sleeps/real waits; real subprocess/server | 89 subprocess uses and 21 sleeps: starts child processes and checks none is left behind (50 s). | cloud lane |
| 37 | TestResume | bin/test-switchboard | 45.1 | mutant runs | one test applies every mutant in memory and runs its named test (25 s). | switchboard (bin/) |
| 38 | test_cloud_k1.py | cloud/launcher/tests/test_cloud_k1.py | 45.0 | big fixture/setup | fake cluster per scenario (45 s). | cloud lane |
| 39 | TestOneTableOfTheBinSetClasses | bin/test-path-suites | 44.0 | real subprocess/server; big fixture/setup | each test builds a fixture repository and runs `preflight-pr` as a subprocess (up to 20 s). | switchboard (bin/) |
| 40 | test_scratch_clean.py | cloud/launcher/tests/test_scratch_clean.py | 43.0 | sleeps/real waits; real subprocess/server | leaves children running and checks they are ended and a stubborn one killed: real process waits. | cloud lane |
| 41 | test_cloud_stack.py | cloud/launcher/tests/test_cloud_stack.py | 43.0 | big fixture/setup | fake kubectl stack-pod shapes, 7 mutant mentions (43 s). | cloud lane |
| 42 | TestHeavyProgress | tests/switchboard/test_heavy_progress.py | 42.4 | sleeps/real waits; real subprocess/server | s14 starts a child that blocks on a lock and waits for the outer bound (24 s); s10 and s11 wait for threads to time out (5 s each). | switchboard (bin/) |
| 43 | test_kept_expiry.py | cloud/launcher/tests/test_kept_expiry.py | 42.0 | big fixture/setup | fake kubectl and fake clock; no sleeps: setup per case (41 s over the file). | cloud lane |
| 44 | TestShimTopSpender | bin/test-switchboard | 41.6 | real subprocess/server | s3 runs the shim with every environment name it reads, one process each (20 s). | switchboard (bin/) |
| 45 | test_cloud_worker.py | cloud/launcher/tests/test_cloud_worker.py | 41.0 | real subprocess/server; big fixture/setup | 13 subprocess uses: launcher, reaper, end hook through the fake kubectl (41 s). | cloud lane |
| 46 | TestCloudChannelServer | bin/test-cloud-channel-server | 39.1 | real subprocess/server; sleeps/real waits | 62 tests that start a real channel server and talk to it. | cloud lane |
| 47 | test_failed_pod_record.py | cloud/launcher/tests/test_failed_pod_record.py | 38.0 | big fixture/setup | 800 lines of fake-cluster scenarios, 28 mutant mentions. | cloud lane |
| 48 | test_stall_structural.py | cloud/launcher/tests/test_stall_structural.py | 38.0 | mutant runs; real subprocess/server | 6 mutant mentions, 4 subprocess uses (38 s). | cloud lane |
| 49 | test_app_token.py | cloud/launcher/tests/test_app_token.py | 38.0 | real subprocess/server; mutant runs | real openssl and a loopback stub of the GitHub API; 24 mutant mentions (38 s). | cloud lane |
| 50 | TestIdentityCheck | bin/test-switchboard | 37.5 | real subprocess/server | 41 tests of the identity check through the real shim; one test is 11.7 s (every write refused after a failed lookup). | switchboard (bin/) |

### The slowest 50 individual tests of `bin/test-switchboard` (pod seconds)

Per-test times of the other suites are in the JSON (`other_suites[...].top_tests`); the slowest of them: `bin/test-cloud-cli` `TestRefusals.test_error_body_cut_mid_read` 31.7 s and `Wire.test_stalled_server_is_cut_by_the_timeout` 30.1 s (real socket timeouts), `bin/test-path-suites` `test_s3_with_merged_the_same_one_line_per_row` 20.6 s, `bin/test-fleet-doctor` `test_the_line_present_exactly_once_passes_and_every_other_state_fails` 13.9 s.

| # | seconds | test | tag |
|---:|---:|---|---|
| 1 | 160.7 | test_launch_capacity.TestLaunchCapacity.test_u23_a_mutant_whose_windows_do_not_overlap_fails_with_the_text | mutant runs |
| 2 | 103.0 | __main__.TestBlockedMutants.test_every_guard_negated_is_killed_by_its_named_test | mutant runs |
| 3 | 100.6 | test_host_disk.TestHostDisk.test_the_named_group_passes_with_the_host_at_5_gb | real subprocess/server; big fixture/setup |
| 4 | 96.2 | __main__.TestFactsReadAtRunTime.test_986_a_new_fact_read_in_any_spelling_in_any_function_fails_the_run_time_test | real subprocess/server/big fixture/setup |
| 5 | 79.1 | test_launch_capacity.TestLaunchCapacity.test_u23_an_address_at_every_window_boundary_offset_is_masked_whole | mutant runs; real subprocess/server |
| 6 | 64.3 | test_924.TestImpactHold924.test_a_registrar_that_reassigns_a_function_of_the_clear_leaves_the_hold_as_it_was | parse-once candidate; mutant runs |
| 7 | 60.6 | test_cases_10.TestArchitectureSafetyNet.test_the_command_list_covers_every_verb_and_the_recording_is_deterministic | real subprocess/server; big fixture/setup |
| 8 | 58.6 | test_stop_final_message.TestStopFinalMessageBareMutant.test_U20_a_copy_without_the_main_entry_is_seen | mutant runs |
| 9 | 54.1 | test_stop_final_message.TestStopFinalMessageBare.test_U20_the_file_run_directly_runs_and_counts_its_tests | real subprocess/server |
| 10 | 48.6 | test_924.TestImpactHold924.test_a_registrar_that_reassigns_any_function_the_clear_or_the_rating_runs_by_stops_the_command_for_every_action | parse-once candidate; mutant runs |
| 11 | 40.6 | __main__.TestBlockedMutants.test_every_bound_lock_validator_and_key_mutant_is_killed_by_its_named_test | mutant runs |
| 12 | 39.8 | test_924.TestImpactHold924.test_the_rating_roots_are_derived_from_the_code_and_each_is_classified_in_one_table | parse-once candidate; mutant runs |
| 13 | 36.6 | test_launched_reviewers.TestLaunchedReviewerMutants.test_s7_each_mutant_is_applied_once_fails_its_named_guard_with_the_text_of_the_check_and_its_control_passes | mutant runs |
| 14 | 34.9 | test_queued_behind_turn.TestQueuedBehindTurn.test_s5_the_mutants_fail_with_the_anchored_texts_after_the_controls_pass | mutant runs |
| 15 | 33.2 | test_plan_hash_marker.TestPlanHashMarkerLaunch.test_a_mutant_that_reads_the_consensus_without_as_current_is_refused_at_the_old_hash | mutant runs |
| 16 | 32.2 | test_plan_hash_marker.TestPlanHashMarkerLaunch.test_the_control_an_unmutated_copy_of_the_entry_is_quiet_for_the_old_hash | real subprocess/server/big fixture/setup |
| 17 | 29.5 | __main__.TestRegistrarFloor.test_the_table_holds_on_the_real_lint_and_every_mutant_of_it_fails_a_row | mutant runs |
| 18 | 26.4 | __main__.TestOfflineScenarios.test_scenario_q1_quiet_two_hours | big fixture/setup |
| 19 | 24.7 | __main__.TestResume.test_every_mutant_is_killed_by_its_named_test | mutant runs |
| 20 | 24.1 | test_heavy_progress.TestHeavyProgress.test_s14_a_stop_that_waits_on_a_blocked_write_is_killed_by_the_outer_bound_and_named | mutant runs |
| 21 | 23.1 | __main__.TestMadeUpPids.test_every_class_that_stands_in_for_a_process_passes_when_its_old_numbers_are_the_real_parent_and_grandparent_from_both_ends_of_the_range | real subprocess/server/big fixture/setup |
| 22 | 22.5 | __main__.TestSupportRebinding.test_every_rebinding_of_a_moved_name_in_this_file_also_rebinds_it_in_the_support_modules | real subprocess/server/big fixture/setup |
| 23 | 22.4 | test_gh_shim_body.TestGhBodyMutants.test_the_control_passes_and_each_mutant_fails_with_its_anchored_text | mutant runs |
| 24 | 21.9 | test_gh_shim_sign_input.TestGhShimEffectRule.test_r4_mutant_3_the_parser_sees_a_mutation_in_every_case_that_reaches_it | mutant runs |
| 25 | 20.8 | test_architecture.TestArchitectureDocument.test_the_code_obeys_the_document | real subprocess/server; big fixture/setup |
| 26 | 20.2 | __main__.TestShimTopSpender.test_s3_every_environment_name_the_shim_reads_is_classified_and_none_switches_the_wait_off_or_changes_whose_lane_it_is | real subprocess/server/big fixture/setup |
| 27 | 18.5 | __main__.TestImpactHold.test_a_change_to_the_code_that_writes_or_parses_the_rating_is_the_owners_own_clear_one_case_for_each_root_and_each_definition_that_touches_a_rating_field | real subprocess/server/big fixture/setup |
| 28 | 17.6 | test_gh_shim_sign_input.TestGhShimEffectRule.test_r4_mutant_1_the_allow_list_check_skipped_is_seen | mutant runs |
| 29 | 17.5 | test_gh_shim_sign_input.TestGhShimEffectRule.test_r4_mutant_2_implicit_post_treated_as_get_is_seen | mutant runs |
| 30 | 17.5 | test_gh_shim_sign_input.TestGhShimEffectRule.test_r4_mutant_4_the_unknown_body_refusal_removed_is_seen | mutant runs |
| 31 | 17.3 | test_queued_behind_turn.TestQueuedBehindTurn.test_the_owners_light_switch_means_the_queued_wait_pass_reads_no_inbox | real subprocess/server/big fixture/setup |
| 32 | 16.9 | test_gh_shim_sign_input.TestGhShimEffectRule.test_r4_the_battery_passes_for_the_real_shim | real subprocess/server/big fixture/setup |
| 33 | 16.8 | test_gh_shim_sign_input.TestGhShimEffectRule.test_r4_mutant_5_canonicalisation_skipped_is_seen | mutant runs |
| 34 | 16.8 | test_cases_12.WorkflowPlanPins.test_every_mutant_fails_a_pin | mutant runs |
| 35 | 16.7 | test_new_test_checks.TestNewTestChecksMutants.test_mutant_preflight_pr_without_the_call | mutant runs |
| 36 | 16.5 | test_new_test_checks.TestNewTestChecksMutants.test_mutant_cloud_preflight_without_the_call | mutant runs |
| 37 | 14.9 | test_end_on_ask.TestEndOnAsk.test_quoted_and_invisible_folds_many_blank_lines_and_the_cost_of_a_long_text | real subprocess/server/big fixture/setup |
| 38 | 14.8 | __main__.TestOfflineScenarios.test_scenario_g3_hook_already_exists | big fixture/setup |
| 39 | 14.0 | test_stop_final_message.TestStopFinalMessageHardeningMutant.test_U11_to_U14_the_control_passes_and_each_mutant_fails_with_the_text_of_the_check | mutant runs |
| 40 | 13.9 | test_new_test_checks.TestNewTestChecksMutants.test_mutant_table_list_that_stops_listing | mutant runs |
| 41 | 13.8 | test_new_test_checks.TestNewTestChecksMutants.test_mutant_import_time_read_that_stops_flagging | mutant runs |
| 42 | 13.8 | test_new_test_checks.TestNewTestChecksMutants.test_mutant_part_module_that_stops_flagging | mutant runs |
| 43 | 13.7 | test_new_test_checks.TestNewTestChecksMutants.test_mutant_moved_name_that_stops_flagging | mutant runs |
| 44 | 13.1 | test_new_test_checks.TestNewTestChecksFiles.test_s8_the_existing_test_files_and_this_one_give_no_finding | real subprocess/server/big fixture/setup |
| 45 | 12.6 | test_shim_guards.TestShimGuardsTool.test_s6_the_six_existing_modules_demand_nothing | real subprocess/server/big fixture/setup |
| 46 | 12.5 | test_launch_capacity.TestLaunchCapacity.test_u21_a_mutant_that_goes_back_to_the_pattern_alone_fails_with_the_text | mutant runs |
| 47 | 12.5 | test_launch_capacity.TestLaunchCapacity.test_u21_generated_lines_leak_nothing_and_the_fixed_cases_hold | real subprocess/server/big fixture/setup |
| 48 | 12.3 | test_gh_shim_sign_input.TestGhShimRawPath.test_mutant_override_check_skipped_is_seen | mutant runs |
| 49 | 11.9 | test_gh_shim_sign_input.TestGhShimSignInput.test_r3_each_graphql_spelling_is_refused_with_the_one_line | real subprocess/server/big fixture/setup |
| 50 | 11.7 | __main__.TestIdentityCheck.test_a_failed_account_lookup_refuses_every_write_and_lets_every_read_through | real subprocess/server/big fixture/setup |
## 5. What the 4 November analysis (ask #2108) changed

Then: run 41793485239 (4 November 2026, green, `test (ubuntu-latest)` ~70 minutes, one process): `bin/test-switchboard` 61.2 minutes, 8,696 tests, 712 classes; top 20 classes 38.6 minutes (63%; the 20 rows the plan lists sum to 37.6, 61%, the table below uses those rows); minutes per class from log timestamps (time between consecutive test lines). Today: pod seconds per class, summed per-test times from this baseline; 10,903 tests, 1,018 classes; the 8 shards (slices F-A..F-D and the shard option landed). Then-minutes and today-seconds come from different machines and different methods (log timestamps vs per-test timers), so differences under about a third mean nothing.

### The 20 classes of then, today

| class | then (min) | then tests | today (s) | today (min) | today tests | change |
|---|---:|---:|---:|---:|---:|---|
| TestRegistrarFloor | 5.9 | 45 | 106.8 | 1.8 | 45 | faster 3.3x |
| TestWiredCheckPlanted | 3.4 | 18 | 10.0 | 0.2 | 19 | faster 20.5x |
| TestNoPaneToJudge | 3.2 | 23 | 7.3 | 0.1 | 23 | faster 26.2x |
| TestHostDisk | 2.6 | 6 | 105.7 | 1.8 | 7 | about the same |
| TestArchitectureSafetyNet | 2.5 | 17 | 62.9 | 1.0 | 17 | faster 2.4x |
| TestMutantIsCaught | 2.4 | 9 | 23.4 | 0.4 | 5 | faster 6.2x |
| TestImpactHold | 2.2 | 121 | 65.8 | 1.1 | 120 | faster 2.0x |
| TestTheEntryGuard | 2.1 | 29 | 16.4 | 0.3 | 29 | faster 7.7x |
| TestHeavyProgress | 1.8 | 25 | 42.4 | 0.7 | 25 | faster 2.5x |
| TestOfflineScenarios | 1.7 | 76 | 91.1 | 1.5 | 76 | about the same |
| TestWatcher | 1.5 | 6 | 8.0 | 0.1 | 4 | faster 11.3x |
| TestWholeRead | 1.4 | 22 | 3.1 | 0.1 | 22 | faster 27.2x |
| TestBlockedMutants | 1.1 | 4 | 146.9 | 2.4 | 4 | slower 2.2x |
| TestTheBinSetClassesAreAllInTheTable | 0.9 | 8 | 3.4 | 0.1 | 8 | faster 15.8x |
| TestFactsReadAtRunTime | 0.9 | 2 | 97.5 | 1.6 | 2 | slower 1.8x |
| TestMadeUpPids | 0.9 | 17 | 29.5 | 0.5 | 17 | faster 1.8x |
| TestShimTopSpender | 0.8 | 15 | 41.6 | 0.7 | 15 | about the same |
| TestGuardSet | 0.8 | 7 | 22.2 | 0.4 | 7 | faster 2.2x |
| TestImpactReleaseEveryPass | 0.8 | 70 | 13.3 | 0.2 | 70 | faster 3.6x |
| TestOfflineScenariosPR4 | 0.7 | 10 | 3.7 | 0.1 | 10 | faster 11.4x |

The 20 then-classes: 37.6 minutes then (the sum of the 20 rows as listed in the plan; the plan itself states 38.6, a 1.0 minute difference I cannot explain) and 15.0 minutes today. A change of under a third in either direction is called `about the same` (ratio between 0.67 and 1.5).

### The top 20 today

| # | class | file | tests | seconds | minutes | in the then top 20? |
|---:|---|---|---:|---:|---:|---|
| 1 | TestLaunchCapacity | tests/switchboard/test_launch_capacity.py | 47 | 299.1 | 5.0 | no |
| 2 | TestImpactHold924 | tests/switchboard/test_924.py | 61 | 203.1 | 3.4 | no |
| 3 | TestBlockedMutants | bin/test-switchboard | 4 | 146.9 | 2.4 | yes |
| 4 | TestGhShimEffectRule | tests/switchboard/test_gh_shim_sign_input.py | 12 | 119.2 | 2.0 | no |
| 5 | TestRegistrarFloor | bin/test-switchboard | 45 | 106.8 | 1.8 | yes |
| 6 | TestHostDisk | tests/switchboard/test_host_disk.py | 7 | 105.7 | 1.8 | yes |
| 7 | TestFactsReadAtRunTime | bin/test-switchboard | 2 | 97.5 | 1.6 | yes |
| 8 | TestOfflineScenarios | bin/test-switchboard | 76 | 91.1 | 1.5 | yes |
| 9 | TestNewTestChecksMutants | tests/switchboard/test_new_test_checks.py | 6 | 88.4 | 1.5 | no |
| 10 | TestGhShimSignInput | tests/switchboard/test_gh_shim_sign_input.py | 38 | 85.3 | 1.4 | no |
| 11 | TestStopFinalMessageBareMutant | tests/switchboard/test_stop_final_message.py | 2 | 65.8 | 1.1 | no |
| 12 | TestImpactHold | bin/test-switchboard | 120 | 65.8 | 1.1 | yes |
| 13 | TestPlanHashMarkerLaunch | tests/switchboard/test_plan_hash_marker.py | 5 | 65.6 | 1.1 | no |
| 14 | TestStopFinalMessageBare | tests/switchboard/test_stop_final_message.py | 6 | 65.2 | 1.1 | no |
| 15 | TestArchitectureSafetyNet | tests/switchboard/test_cases_10.py | 17 | 62.9 | 1.0 | yes |
| 16 | TestPlanReviewCommands | bin/test-switchboard | 53 | 59.8 | 1.0 | no |
| 17 | TestQueuedBehindTurn | tests/switchboard/test_queued_behind_turn.py | 19 | 55.2 | 0.9 | no |
| 18 | TestResume | bin/test-switchboard | 27 | 45.1 | 0.8 | no |
| 19 | TestHeavyProgress | tests/switchboard/test_heavy_progress.py | 25 | 42.4 | 0.7 | yes |
| 20 | TestShimTopSpender | bin/test-switchboard | 15 | 41.6 | 0.7 | yes |

Today the top 20 classes take 31.9 of 69.6 minutes (46%); then 38.6 of 61.2 as stated (63%; the listed rows sum to 37.6, 61%). In the top 20 today 9 classes were in the then top 20; 11 are new (mostly mutant runners of later asks: `TestLaunchCapacity`, `TestImpactHold924`, `TestGhShimEffectRule`, `TestNewTestChecksMutants`, `TestGhShimSignInput`).

What changed, plainly: (a) the file no longer runs in one process in CI: the 8 shards cut the longest single job from about 70 minutes to 11-19 minutes (shard 4 of today's three runs). (b) Total test work did not fall: it rose from 61.2 to 69.6 minutes (tests 8,696 to 10,903). (c) The slices fixed what they named in part: `TestRegistrarFloor` 5.9 to 1.8 minutes, `TestWiredCheckPlanted` 3.4 to 0.2, `TestNoPaneToJudge` 3.2 to 0.1, `TestWholeRead` 1.4 to 0.05, `TestMutantIsCaught` 2.4 to 0.4, `TestTheEntryGuard` 2.1 to 0.3. (d) Others did not improve or got worse: `TestBlockedMutants` 1.1 to 2.4, `TestFactsReadAtRunTime` 0.9 to 1.6, `TestOfflineScenarios` 1.7 to 1.5, `TestShimTopSpender` 0.8 to 0.7 and the `TestHostDisk` 2.6 to 1.8 minutes remain; `TestHostDisk`'s one 100-second test still re-runs other classes. (e) New slow classes appeared after 4 November (mutant runners of the later asks), and the class-time table `tests/switchboard/class_times.json` was not refreshed, so the shards are unbalanced (shard 3: 2,430 tests; shard 4: 982 s).

## 6. What could not be measured

- **No green run exists.** All three CI runs are red, so "the whole run took N minutes" is the time to the last job end of a failing run; in two runs the launcher chain was cut by its 20-minute timeout, so the true uncut time of the launcher chain on ubuntu CI is not known (it is at least 20.2 minutes; the pod time of the full chain is in section 3).
- **Per-test and per-class times in CI** are not printed by the suites (verbosity lines carry no time), so all class, file and test times are from the pod; the CI columns are step totals only. Runner speeds differ (CI runner vs this pod); the pod's shard totals are 0.95x to 1.13x of the CI shard totals (mean of three runs) for the same shards, so the two sources agree within the run-to-run spread, but they are not the same measurement.
- **The switchboard shards ran 3 at a time** in the pod (6 CPU limit), not one at a time, so each shard has some contention; the other suites ran strictly one at a time. Because of that, the per-test seconds of the switchboard are an upper bound for a quiet machine, not a precise value.
- **The pod has failures that CI does not:** `TestCloudReaperInstallOnMacOS` and the service-file-mode tests (115 failing/erroring lines in shards 3, 6 and 8; the pod's checkout directory is group-writable), `bin/test-app-mint` (1 test: this pod's `gh` client has no `auth git-credential`), `bin/test-cloud-worker` (1 test: `test_the_kits_own_launcher_link_is_accepted`), `cloud/tests/test-cloud` (skip list differs: no `kubectl`). Their timings are kept; their pass/fail is not claimed.
- **`bin/check-public-names`** needs the repository secret: in the pod it printed its skip note (0.03 s), so its real cost (6-8 s in CI) was not measured here, and its CI failure is not examined beyond the fact of the failure.
- **Launcher entries on the parallel list** print their TIME only when their lane output is replayed at the end; in a timed-out CI run none of them were replayed, so their CI times are unknown; the pod times are from the full run (193 entries, 2,002 s = 33.4 minutes of wall with 3 parallel lanes; 192 TIME lines; `test_launcher_shards.py` failed in the pod and prints no TIME line: it takes about 2 s in CI).
- **macOS legs** were not measured in the pod (no macOS here); CI times of the macOS jobs are in section 1 only as job minutes.
- **The nightly legs** (`nightly <ref> (...)` jobs that run the whole `bin/test-switchboard` unsharded on ubuntu) are not in the last three runs (all three were dispatches); not measured.
- **Per-test times inside the launcher files** (one process per file, no common runner) were not collected; only per-file times from the chain's TIME lines.

## How this was measured

- CI: `gh api repos/adamkaplan/fleet-kit/actions/workflows/test.yml/runs?branch=integration` (the last three runs by creation time), `.../actions/runs/<id>/jobs` for job and step times, `gh run logs <id>` for the launcher TIME lines and failure reasons.
- Pod: a small wrapper outside the repository (it runs the unmodified `bin/test-switchboard --shard I/8` and the other suites through `runpy`, replacing `unittest.TextTestRunner.resultclass` by a subclass that records each test's `perf_counter` time, `FLEET_HEAVY=off`; the launcher chain ran as CI runs it, `bin/test-cloud-launcher`, with `CLOUD_TEST_SKIP_DOCKER=1`). Python 3.12.3. The wrapper is not committed (this pull request holds only the docs and the data file).
- Owner column: the task puts `bin/` switchboard tests in this lane and `cloud/launcher`, `cloud/worker` tests in the cloud lane; I assigned every `bin/test-cloud-*` suite, `bin/test-decisions-pull`, `cloud/tests/test-cloud` and `bin/test-cloud-neutral-names` to the cloud lane, and the plugin node tests and the other `bin/test-*` suites to the switchboard lane. A class inside `bin/test-switchboard` that tests cloud code (for example `TestLaunchCapacity`, `TestCloudReaperInstallOnMacOS`) is listed under the switchboard lane because its file is a switchboard test file; the lanes can reassign.