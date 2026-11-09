# Pin sweep for ask #2172
Derived by: `git log --since=2026-11-04T00:00:00Z --name-only --format= origin/integration -- 'tests/**' 'bin/test-*' 'cloud/**/tests/**' | sort -u`
Note: tests/switchboard/test_first_push_notice.py (2 tests) and test_ci_fresh.py (1 test) fail at the base too, unrelated to this sweep.
bin/test-cloud-launcher.chain: none (read)
bin/test-cloud-launcher.parallel: none (read)
bin/test-cloud-neutral-names: none (read)
bin/test-cloud-relay-daemon: none (read)
bin/test-cloud-worker: rewritten here
bin/test-fleet-doctor: rewritten here
bin/test-gh-hold: rewritten in #2185
bin/test-switchboard: rewritten in #2185
bin/test-workflow-split: none (read)
cloud/launcher/tests/chain_unmeasured.list: none (read)
cloud/launcher/tests/fake_kubectl: none (read)
cloud/launcher/tests/fixtures/default_pod.json: none (read)
cloud/launcher/tests/fixtures/run_secret_pod.json: none (read)
cloud/launcher/tests/fixtures/stack_pod.json: none (read)
cloud/launcher/tests/fixtures/stack_pod_registry.json: none (read)
cloud/launcher/tests/test_app_token.py: none (read)
cloud/launcher/tests/test_app_token_route.py: rewritten in #2185
cloud/launcher/tests/test_brief_header.py: rewritten in #2185
cloud/launcher/tests/test_cloud_docs_wording.py: none (read)
cloud/launcher/tests/test_cloud_end_alert.py: none (read)
cloud/launcher/tests/test_cloud_inspect.py: rewritten in #2185
cloud/launcher/tests/test_cloud_k1.py: none (read)
cloud/launcher/tests/test_cloud_model_served.py: rewritten in #2185
cloud/launcher/tests/test_cloud_prestop.py: none (read)
cloud/launcher/tests/test_cloud_settings.py: none (read)
cloud/launcher/tests/test_cloud_split.py: rewritten in #2185
cloud/launcher/tests/test_cloud_turn_end.py: none (read)
cloud/launcher/tests/test_cloud_worker.py: none (read)
cloud/launcher/tests/test_create_says_why.py: rewritten in #2185
cloud/launcher/tests/test_cw_names.py: none (read)
cloud/launcher/tests/test_end_hook_pr_ready.py: none (read)
cloud/launcher/tests/test_failed_pod_record.py: rewritten in #2185
cloud/launcher/tests/test_final_note.py: none (read)
cloud/launcher/tests/test_hub_entry_keys.py: rewritten in #2185
cloud/launcher/tests/test_idle_default_note.py: rewritten in #2185
cloud/launcher/tests/test_idle_nudge.py: rewritten in #2185
cloud/launcher/tests/test_issues_read.py: none (read)
cloud/launcher/tests/test_kept_failed_once.py: none (read)
cloud/launcher/tests/test_m_cap.py: none (read)
cloud/launcher/tests/test_no_open_files.py: none (read)
cloud/launcher/tests/test_orchestrator_agent.py: rewritten in #2185
cloud/launcher/tests/test_orchestrator_hub_session.py: none (read)
cloud/launcher/tests/test_persistent_merge_fix.py: none (read)
cloud/launcher/tests/test_persistent_orchestrator.py: none (read)
cloud/launcher/tests/test_pod_state.py: rewritten in #2185
cloud/launcher/tests/test_preflight.py: none (read)
cloud/launcher/tests/test_preflight_repo_read.py: rewritten in #2185
cloud/launcher/tests/test_reap_concurrent.py: rewritten in #2185
cloud/launcher/tests/test_reaper_audit_tail.py: rewritten in #2185
cloud/launcher/tests/test_registry_endpoint.py: rewritten in #2185
cloud/launcher/tests/test_repo_min_shape.py: rewritten in #2185
cloud/launcher/tests/test_retire_gone_exact.py: rewritten in #2185
cloud/launcher/tests/test_safe_to_evict.py: rewritten in #2185
cloud/launcher/tests/test_scrub_linear.py: rewritten in #2185
cloud/launcher/tests/test_scrub_pieces.py: none (read); kept: planted-values-left == 0 and "a %s here" byte-equal plain text (112): scrub must not change plain words
cloud/launcher/tests/test_stop_cause.py: none (read)
cloud/launcher/tests/test_token_at_limit.py: none (read)
cloud/launcher/tests/test_vault_block.py: rewritten in #2185
cloud/worker/tests/golden/orchestrator.md: none (read)
cloud/worker/tests/test_activity.py: none (read)
cloud/worker/tests/test_comment_by_id.py: rewritten here
cloud/worker/tests/test_dockerfile_browser.py: none (read)
cloud/worker/tests/test_identity_image.py: rewritten in #2185
cloud/worker/tests/test_plan_check_in_pod.py: none (read)
cloud/worker/tests/test_pod_inspect.py: rewritten in #2185
cloud/worker/tests/test_question_denied.py: none (read)
cloud/worker/tests/test_shell_check.py: rewritten in #2185
tests/ci-test-files.txt: none (read)
tests/golden/surface-dir-sb.txt: none (read)
tests/platform-classes.txt: none (read)
tests/switchboard/class_times.json: none (read)
tests/switchboard/fixtures/queued_behind_turn_idle_headers.json: none (read)
tests/switchboard/fixtures/queued_behind_turn_other_headers.json: none (read)
tests/switchboard/support/core.py: none (read)
tests/switchboard/support/fake_hubs.py: none (read)
tests/switchboard/support/herdr_world.py: none (read)
tests/switchboard/support/offline_fakes.py: none (read)
tests/switchboard/support/orders_and_adapters.py: none (read)
tests/switchboard/support/policy_and_doc_tables.py: none (read)
tests/switchboard/support/scenario_roles.py: none (read)
tests/switchboard/support/wire_fakes.py: none (read)
tests/switchboard/test_1041.py: rewritten in #2185
tests/switchboard/test_1241.py: rewritten in #2185
tests/switchboard/test_bin_set_table.py: none (read)
tests/switchboard/test_body_check.py: rewritten in #2185
tests/switchboard/test_branch_ff.py: rewritten in #2185
tests/switchboard/test_cases_02.py: none (read)
tests/switchboard/test_cases_03.py: none (read)
tests/switchboard/test_cases_05.py: none (read)
tests/switchboard/test_cases_08.py: none (read)
tests/switchboard/test_cases_09.py: none (read)
tests/switchboard/test_cases_10.py: none (read)
tests/switchboard/test_cases_11.py: rewritten here
tests/switchboard/test_cases_12.py: none (read)
tests/switchboard/test_charter_noparent.py: none (read)
tests/switchboard/test_charter_other_repo.py: none (read)
tests/switchboard/test_ci_fresh.py: rewritten here; kept byte-equal: the stale/refusal sentences (lines 575-577), the ci_line "fresh: CI ran on the tip" (590) and the verdict evidence text (600): the plan of the ask requires these texts as behaviour; kept ordered read lists (313, 316, 561): the S7 clause is "two reads at the tip, three behind, in order"
tests/switchboard/test_ci_outside_bin.py: rewritten in #2185
tests/switchboard/test_ci_record_proof.py: rewritten in #2185
tests/switchboard/test_claim_legacy.py: rewritten in #2185
tests/switchboard/test_claims_note.py: rewritten in #2185
tests/switchboard/test_closure.py: none (read)
tests/switchboard/test_cloud_send_state.py: none (read)
tests/switchboard/test_cockpit.py: rewritten in #2185
tests/switchboard/test_comment_cursor.py: none (read)
tests/switchboard/test_dead_verdict.py: rewritten in #2185
tests/switchboard/test_dead_verdict_daemon.py: rewritten in #2185
tests/switchboard/test_delivery_lag.py: none (read)
tests/switchboard/test_disk_guard_prune.py: none (read)
tests/switchboard/test_done_when_list.py: rewritten here
tests/switchboard/test_epic_record.py: none (read)
tests/switchboard/test_errored_turn.py: rewritten in #2185
tests/switchboard/test_evidence_limit.py: rewritten in #2185
tests/switchboard/test_first_push_notice.py: rewritten here; kept: the once-only send counts (len(sent)==1): the clause is one notice per pod
tests/switchboard/test_gh_shim_sign.py: rewritten in #2185
tests/switchboard/test_gh_shim_sign_input.py: rewritten in #2185
tests/switchboard/test_gh_shim_sign_repo_flag.py: rewritten in #2185
tests/switchboard/test_guard_set.py: rewritten here
tests/switchboard/test_header_cache.py: none (read)
tests/switchboard/test_header_date.py: rewritten in #2185
tests/switchboard/test_heavy_holders.py: none (read)
tests/switchboard/test_heavy_progress.py: none (read)
tests/switchboard/test_heavy_split.py: rewritten in #2185
tests/switchboard/test_held_sends.py: rewritten in #2185
tests/switchboard/test_host_disk.py: none (read)
tests/switchboard/test_intent_told_once.py: none (read)
tests/switchboard/test_keyed_reminder_supersede.py: rewritten in #2185
tests/switchboard/test_launch_capacity.py: rewritten in #2185
tests/switchboard/test_launched_reviewers.py: rewritten in #2185
tests/switchboard/test_lost_worker.py: none (read)
tests/switchboard/test_merge_drops.py: none (read)
tests/switchboard/test_merge_gate_answer.py: rewritten here
tests/switchboard/test_new_module_demands.py: rewritten in #2185
tests/switchboard/test_new_test_checks.py: rewritten in #2185
tests/switchboard/test_open_list_ceiling.py: rewritten in #2185
tests/switchboard/test_open_list_cuts.py: rewritten in #2185
tests/switchboard/test_open_list_pass_calls.py: none (read)
tests/switchboard/test_pass_recheck.py: rewritten in #2185
tests/switchboard/test_pins.py: rewritten in #2185
tests/switchboard/test_plan_apply.py: rewritten here
tests/switchboard/test_plan_failure_mode.py: rewritten in #2185
tests/switchboard/test_plan_wake_drops.py: rewritten in #2185
tests/switchboard/test_platform_classes.py: none (read)
tests/switchboard/test_pod_niceness.py: none (read)
tests/switchboard/test_pod_silence.py: none (read); kept: told-once vs told-again counts (65-69, 135-168): the mutant-vs-control contrast is the behaviour
tests/switchboard/test_pr_state_jobs.py: rewritten in #2185
tests/switchboard/test_push_watch.py: rewritten in #2185
tests/switchboard/test_queued_behind_turn.py: rewritten in #2185
tests/switchboard/test_read_pages_strict.py: none (read)
tests/switchboard/test_reclaim_cap.py: rewritten in #2185
tests/switchboard/test_relay_loop_cadence.py: none (read)
tests/switchboard/test_reminder_waiting.py: rewritten in #2185
tests/switchboard/test_scratch_command.py: rewritten in #2185
tests/switchboard/test_sent_keys_age.py: rewritten in #2185
tests/switchboard/test_shards.py: rewritten in #2185
tests/switchboard/test_stage_events.py: rewritten in #2185
tests/switchboard/test_status_shape.py: none (read)
tests/switchboard/test_stop_final_message.py: rewritten in #2185
tests/switchboard/test_tester_agent.py: rewritten in #2185
tests/switchboard/test_triage_parked_digest.py: none (read)
tests/switchboard/test_unconsumed_record.py: rewritten in #2185
tests/switchboard/test_unconsumed_steer.py: rewritten in #2185
tests/switchboard/test_verdict_evidence.py: rewritten in #2185
tests/switchboard/test_wired_tests.py: none (read)
tests/test_doctor_watcher.py: none (read)
tests/test_installed_guard.py: none (read)
