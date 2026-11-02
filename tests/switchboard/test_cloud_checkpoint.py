"""#1790: a successful `launch --cloud` arms the checkpoint reminder and the overdue wake (a CHECKPOINT line or flags as a local launch, else the default 25m/40m).
The cloud launcher is a fake (`run_cloud` patched): it keeps the brief text on args as the real one does and returns success."""
import os
from unittest import mock
from pathlib import Path


def build(TwoRepoCase, sb, EPOCH, ACME_WEB):
    class TestCloudLaunchArmsCheckpoint(TwoRepoCase):
        def fake_cloud(self, function, args, runner, now):
            args.brief_text = Path(args.brief_file).read_text() if args.brief_file else ""
            return sb.EXIT_OK

        def launch(self, *extra, brief=None):
            argv = ["launch", "web-new", "--cloud", "--repo", ACME_WEB, "--reports-to", "web-orch", "--issue", "acme/web#2", *extra]
            if brief is not None:
                (self.home / "brief.txt").write_text(brief)
                argv += ["--brief-file", str(self.home / "brief.txt")]
            with mock.patch.object(sb, "run_cloud", self.fake_cloud):
                return self.main(*argv)

        def reminders(self):
            return {r["to"]: r for r in sb.read_reminders(self.world.state)}

        def audit_for(self):
            return sorted(e["detail"]["for"] for e in self.world.audit() if e["event"] == "reminder.add")

        def check_default(self, launch=None):
            code, _, err = (launch or self.launch)()
            self.assertEqual(code, 0, err)
            found = self.reminders()
            self.assertEqual(len(found), 2, "default 25m/40m: 2 reminders expected")
            self.assertEqual((found["web-new"]["at"] - EPOCH, found["web-orch"]["at"] - EPOCH), (1500, 2400), "default 25m/40m: times")
            self.assertIn("default 25m/40m", err, "the printed line says default 25m/40m")
            self.assertEqual(self.audit_for(), ["checkpoint-overdue", "checkpoint-worker"])

        def test_s2_neither_line_nor_flag_arms_the_default(self):
            self.check_default()

        def test_s3_the_line_then_the_flags(self):
            code, _, err = self.launch(brief="CHECKPOINT: at 20 minutes, overdue at 45")
            self.assertEqual(code, 0, err)
            found = self.reminders()
            self.assertEqual((len(found), found["web-new"]["at"] - EPOCH, found["web-orch"]["at"] - EPOCH), (2, 1200, 2700))
            self.assertNotIn("default", err)

        def test_s3_flags_win(self):
            code, _, err = self.launch("--checkpoint", "10m", "--overdue", "30m", brief="CHECKPOINT: at 20 minutes, overdue at 45")
            self.assertEqual(code, 0, err)
            found = self.reminders()
            self.assertEqual((len(found), found["web-new"]["at"] - EPOCH, found["web-orch"]["at"] - EPOCH), (2, 600, 1800))
            self.assertNotIn("default", err)

        def test_s4_an_unreadable_line_arms_the_default_and_says_so(self):
            code, _, err = self.launch(brief="CHECKPOINT: when done")
            self.assertEqual(code, 0, err)
            self.assertEqual(len(self.reminders()), 2)
            self.assertIn("could not be read", err)
            self.assertIn("default 25m/40m", err)

        def test_s5_both_are_armed_with_their_conditions(self):
            self.assertEqual(self.launch()[0], 0)
            found = self.reminders()
            self.assertEqual(found["web-new"]["condition"], {"while_running": "web-new"})
            self.assertEqual(found["web-orch"]["condition"]["unless_report_from"], "web-new")

        def test_s5_no_launcher_known_means_no_overdue(self):
            os.environ.pop("HERDR_PANE_ID", None)
            argv = ["launch", "web-new", "--cloud", "--repo", ACME_WEB, "--issue", "acme/web#2"]
            with mock.patch.object(sb, "run_cloud", self.fake_cloud):
                code, _, err = self.main(*argv)
            self.assertEqual(code, 0, err)
            self.assertEqual(set(self.reminders()), {"web-new"})
            self.assertIn("no overdue reminder", err)

        def test_s6_a_mutant_that_drops_the_default_fails_the_check(self):
            self.check_default()   # the control: the unmutated launch passes first
            sb.write_reminders(self.world.state, [])
            with mock.patch.object(sb, "arm_cloud_checkpoints", lambda *a, **k: sb.EXIT_OK):
                with self.assertRaises(AssertionError) as failed:
                    self.check_default()
            self.assertIn("default 25m/40m", str(failed.exception))

        def test_u1_a_local_launch_is_unchanged(self):
            os.environ.pop("HERDR_PANE_ID", None)
            with mock.patch.object(sb, "arm_cloud_checkpoints", side_effect=AssertionError("a local launch never arms the cloud default")):
                work = self.home / "checkout"
                work.mkdir(exist_ok=True)
                code, _, err = self.main("launch", "web-new", "--agent", "coder", "--dir", str(work), "--workspace", "w1", "--role", "coder",
                                         "--reports-to", "web-orch", "--repo", ACME_WEB)
            self.assertEqual((code, err, sb.read_reminders(self.world.state)), (0, "", []))

        def test_u2_an_arming_that_fails_never_fails_the_launch(self):
            with mock.patch.object(sb, "add_reminder", side_effect=OSError("read-only store")):
                code, _, err = self.launch()
            self.assertEqual(code, 0, err)
            self.assertEqual(len([l for l in err.splitlines() if "no checkpoint reminders" in l]), 1)

        def places(self):
            root = Path(__file__).resolve().parents[2]
            launch_help = sb.build_parser()._subparsers._group_actions[0].choices["launch"].format_help()
            doc = (root / "docs/switchboard.md").read_text()
            return {"the launch help": launch_help,
                    "skills/fleet-planning/SKILL.md": (root / "skills/fleet-planning/SKILL.md").read_text(),
                    "skills/fleet-coordination/SKILL.md": (root / "skills/fleet-coordination/SKILL.md").read_text(),
                    "docs/switchboard.md (Self-wakes)": doc[doc.index("### Self-wakes\n"):]}

        def check_places(self, places):
            missing = [name for name, text in places.items()
                       if not ("default" in text and "25m" in text and "40m" in text and "default 25m/40m" in " ".join(text.split()))]
            self.assertEqual(missing, [], "the derived place lacks the default 25m/40m: %s" % ", ".join(missing))
            return len(places) - len(missing)

        def test_s7_four_places_say_the_default(self):
            self.assertEqual(self.check_places(self.places()), 4)

        def test_s7_a_mutant_in_one_place_names_that_place(self):
            places = self.places()
            self.check_places(places)   # the control passes first
            name = "skills/fleet-coordination/SKILL.md"
            places[name] = places[name].replace("default 25m/40m", "old rule")
            with self.assertRaises(AssertionError) as failed:
                self.check_places(places)
            self.assertIn(name, str(failed.exception))
        def failing_launch(self, how):
            def fake(function, args, runner, now):
                if how == "raise":
                    raise sb.LaunchError("pod creation failed; nothing was created")
                return 1
            with mock.patch.object(sb, "run_cloud", fake):
                return self.main("launch", "web-new", "--cloud", "--repo", ACME_WEB, "--reports-to", "web-orch", "--issue", "acme/web#2")

        def check_failed_launch_arms_nothing(self, how):
            sb.write_reminders(self.world.state, [])
            code, _, err = self.failing_launch(how)
            self.assertEqual(len(sb.read_reminders(self.world.state)), 0, "a failed launch (%s) armed reminders" % how)
            self.assertEqual(code, 1, err)
            self.assertNotIn("default 25m/40m", err)

        def test_a_failed_cloud_launch_exit_arms_nothing(self):
            self.check_failed_launch_arms_nothing("exit")

        def test_a_raised_launch_error_arms_nothing(self):
            self.check_failed_launch_arms_nothing("raise")

        def test_a_stack_launch_arms_the_default_once(self):
            code, _, err = self.launch("--stack")
            self.assertEqual(code, 0, err)
            self.assertEqual(len(sb.read_reminders(self.world.state)), 2)
            self.assertEqual(len([l for l in err.splitlines() if "default 25m/40m" in l]), 1)

        def test_the_outer_handler_of_the_arming_keeps_a_launched_pod_successful(self):
            with mock.patch.object(sb, "build_engine", side_effect=RuntimeError("the engine cannot be built")):
                try:
                    code, _, err = self.launch()
                except RuntimeError as escaped:
                    self.fail("the launch no longer returns success: the RuntimeError escaped (%s)" % escaped)
            self.assertEqual(code, 0, "the launch no longer returns success: %s" % err)
            self.assertEqual(len([l for l in err.splitlines() if "no checkpoint reminders" in l]), 1)
    return TestCloudLaunchArmsCheckpoint
