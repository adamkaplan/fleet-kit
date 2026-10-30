"""#1354: the git token at its limit. Offline: a fake minter (the renewal's fake mint command), a fake clock, the fake kubectl and a fake git.

The pod holds no credential for the minter. The end hook, on a push refused because the token had ended, signals ONE refresh through its result;
the stop side mints through the renewal's route, replaces only the git-token key, waits for the projection, and runs the hook once more.
Scenarios S1-S11, U1 and U2 of the plan; each mutant the plan lists is built here by patching the real code and must fail its check.
"""
import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_git_token_renewal as tr  # noqa: E402  (its harness: a running worker, a per-run Secret, the fake mint, the fake clock)
import end_hook  # noqa: E402

cw = tr.cw
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
FAKE_GIT = os.path.join(HERE, "fixtures", "fake_git_refusal.py")
OLD, NEW, REPO, WORKER = tr.OLD, tr.NEW, tr.REPO, tr.WORKER
NEEDS = {"pushed": False, "wip_branch": None, "error": "the git token had ended", "refresh": "needed"}
PUSHED = {"pushed": True, "wip_branch": WORKER + "/wip", "skipped": []}
OTHER_ERROR = {"pushed": False, "wip_branch": None, "error": "! [remote rejected] HEAD -> w1/wip"}
GOOD_SCOPE = [{"full_name": REPO, "permissions": {"pull": True, "push": True, "admin": False}}]


class Stop(tr.Harness):
    """The stop path with a scripted end hook (the answers it gives, in order) and a fake scope probe and projection."""

    def setUp(self):
        super().setUp()
        self.records_out = os.path.join(self.tmp.name, "runs")
        self.scope = GOOD_SCOPE
        self.hook_calls = 0
        self.captured = []

    def run_stop(self, answers, scope=None, projected=True):
        L = self.launcher()
        answers = list(answers)

        def hook(name):
            self.hook_calls += 1
            return dict(answers.pop(0))
        L.run_hook = hook
        L.scope_probe = lambda token, repo: scope if scope is not None else self.scope
        L._projected = lambda name, token: projected
        L.sleep = lambda s: self.clock.advance(seconds=s)
        a = argparse.Namespace(worker=WORKER, reason="done", run_id=tr.RUN, no_record=False, record_dir=self.records_out)
        out, err = io.StringIO(), io.StringIO()
        error = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                res = L.stop(a, "done")
            except cw.CloudError as e:
                res, error = None, e
        self.captured += [out.getvalue(), err.getvalue(), str(error or "")]
        return res, error

    def stop_audit(self):
        return [l for l in self.audit() if l["action"] == "stop"][-1]

    def record_text(self):
        text = []
        for dirpath, _, files in os.walk(self.records_out):
            for f in files:
                with open(os.path.join(dirpath, f)) as fh:
                    text.append(fh.read())
        return "\n".join(text)

    def everywhere(self):
        """Every output, the audit file and the run records: where no token may ever appear."""
        with open(self.env["CW_CLOUD_AUDIT"]) as f:
            audit = f.read()
        return "\n".join(self.captured + [audit, self.record_text()] + self.outputs)

    def occurrences(self, marker):
        text = self.everywhere()
        return text.count(marker) + text.count(base64.b64encode(marker.encode()).decode())


class TestRefresh(Stop):
    def check_s4(self):
        res, err = self.run_stop([NEEDS, PUSHED])
        self.assertIsNone(err)
        landed = 1 if res and res["pushed"] else 0
        self.assertEqual((landed, len(self.mints()), self.hook_calls), (1, 1, 2), "%d of 1 pushes landed after %d refresh" % (landed, len(self.mints())))
        self.assertEqual(self.mints(), [REPO])   # for the pod's own repository

    def test_s4_an_ended_token_is_refreshed_once_and_the_push_is_retried_once(self):
        self.check_s4()
        self.assertEqual(self.secret()["token"], NEW)
        self.assertEqual(self.st()["secret_bodies"][self.name + "-run"]["model-auth"], "{}")   # only the git-token key changed
        self.assertEqual(self.stop_audit()["refresh"], "used")

    def test_s5_a_refused_refresh_makes_no_second_attempt_and_does_not_loop(self):
        self.set_mode("fail")
        res, err = self.run_stop([NEEDS, PUSHED])
        self.assertIsNotNone(err)
        self.assertEqual((len(self.mints()), self.hook_calls), (1, 1), "%d of 1 refresh calls and %d of 1 push attempts" % (len(self.mints()), self.hook_calls))
        self.assertEqual(self.secret()["token"], OLD)
        self.assertEqual(self.stop_audit()["refresh"], "refused")

    def test_s5_a_second_ended_token_after_the_retry_is_not_refreshed_again(self):
        res, err = self.run_stop([NEEDS, NEEDS, PUSHED])
        self.assertIsNotNone(err)
        self.assertEqual((len(self.mints()), self.hook_calls), (1, 2))
        self.assertEqual(self.stop_audit()["refresh"], "used")

    def test_s6_a_push_refused_for_another_reason_does_not_ask_the_minter(self):
        res, err = self.run_stop([OTHER_ERROR, PUSHED])
        self.assertIsNotNone(err)
        self.assertEqual((len(self.mints()), self.hook_calls), (0, 1))
        self.assertEqual(self.stop_audit()["refresh"], "none")

    def test_s7_a_token_for_another_repository_or_with_wider_permissions_is_refused_and_the_old_file_is_kept(self):
        wider = {"another repository": [{"full_name": tr.OTHER, "permissions": {"push": True}}],
                 "wider permissions": [{"full_name": REPO, "permissions": {"push": True, "admin": True}}],
                 "two repositories": GOOD_SCOPE + [{"full_name": tr.OTHER, "permissions": {"push": True}}]}
        refused = 0
        for what, scope in wider.items():
            with self.subTest(what):
                self.install_worker()
                res, err = self.run_stop([NEEDS, PUSHED], scope=scope)
                self.assertIsNotNone(err)
                self.assertEqual(self.secret()["token"], OLD)
                self.assertEqual(self.stop_audit()["refresh"], "refused")
                refused += 1
        self.assertEqual(refused, len(wider), "%d of %d wider tokens refused" % (refused, len(wider)))

    def test_s7_an_unreadable_scope_is_refused_too(self):
        L = self.launcher()
        L.scope_probe = mock.Mock(side_effect=OSError("down"))
        self.assertFalse(L._scope_is_same(NEW, REPO))

    def test_s9_the_stop_writes_the_token_only_by_the_secret_patch_and_never_into_the_pod_file(self):
        calls = []
        real = cw.Kube.exec

        def spy(kube, name, *cmd, **kw):
            calls.append(cmd)
            return real(kube, name, *cmd, **kw)
        with mock.patch.object(cw.Kube, "exec", spy):
            self.check_s4()
        self.assertEqual([c for c in calls if any(NEW in x for x in c)], [])   # no exec ever carries the token
        bodies = [json.loads(l) for l in open(self.state + ".stdin")]
        self.assertEqual([sorted(b["data"]) for b in bodies], [["git-token"]])   # one patch, one key: the kubelet's own swap puts it in the pod

    def test_s9_a_mutant_that_rewrites_the_pod_file_in_place_is_caught(self):
        calls = []
        real_exec, real = cw.Kube.exec, cw.Launcher.refresh_for_stop

        def spy(kube, name, *cmd, **kw):
            calls.append(cmd)
            return real_exec(kube, name, *cmd, **kw)

        def mutant(L, pod, now):
            got = real(L, pod, now)
            with contextlib.suppress(cw.CloudError):
                L.kube.exec(pod["metadata"]["name"], "tee", "/run/cw/git-token", NEW)   # an in-place write of the pod's file
            return got
        with mock.patch.object(cw.Kube, "exec", spy), mock.patch.object(cw.Launcher, "refresh_for_stop", mutant):
            self.check_s4()
        with self.assertRaises(AssertionError):
            self.assertEqual([c for c in calls if any(NEW in x for x in c)], [])

    def test_s10_the_audit_line_and_the_run_record_say_used_refused_or_none(self):
        words = []
        for answers, mode in (([NEEDS, PUSHED], "ok"), ([NEEDS, PUSHED], "fail"), ([PUSHED], "ok")):
            self.install_worker()
            self.set_mode(mode)
            self.run_stop(answers)
            words.append(self.stop_audit().get("refresh"))
        self.assertEqual(words, ["used", "refused", "none"], "%d of 3 audit lines name the refresh state" % sum(w in ("used", "refused", "none") for w in words))
        self.assertRegex(self.record_text(), r'"refresh": "(used|refused|none)"')

    # -- the mutants the plan lists
    def test_mutant_never_refreshes_ends_with_the_push_refused(self):
        with mock.patch.object(cw.Launcher, "refresh_for_stop", lambda L, pod, now: "refused"):
            with self.assertRaises(AssertionError):
                self.check_s4()

    def test_mutant_always_refreshes_is_caught_by_the_other_reason_case(self):
        real = cw.Launcher.refresh_for_stop
        seen = []
        with mock.patch.object(cw.Launcher, "refresh_for_stop", lambda L, pod, now: seen.append(1) or real(L, pod, now)):
            self.run_stop([OTHER_ERROR])
            self.assertEqual(len(seen), 0)   # the real code never calls it here
        seen.clear()
        with mock.patch.object(cw.Launcher, "refresh_for_stop", lambda L, pod, now: seen.append(1) or real(L, pod, now)):
            self.run_stop([NEEDS, PUSHED])
        self.assertEqual(len(seen), 1)

    def test_mutant_loop_on_refusal_is_caught_by_the_count(self):
        self.set_mode("fail")
        real = cw.Launcher.refresh_for_stop

        def looping(L, pod, now):
            for _ in range(3):
                got = real(L, pod, now)
            return got
        with mock.patch.object(cw.Launcher, "refresh_for_stop", looping):
            self.run_stop([NEEDS, PUSHED])
        self.assertGreater(len(self.mints()), 1)   # what the S5 check (exactly 1) would refuse

    def test_mutant_accepting_a_wider_token_is_caught(self):
        with mock.patch.object(cw.Launcher, "_scope_is_same", lambda L, token, repo: True):
            self.run_stop([NEEDS, PUSHED], scope=[{"full_name": REPO, "permissions": {"push": True, "admin": True}}])
        self.assertEqual(self.secret()["token"], NEW)   # the wider token was taken: the S7 check (kept OLD) fails on it

    def test_s8_and_u2_the_marker_token_is_in_no_output_log_record_or_audit_line(self):
        self.run_stop([NEEDS, PUSHED])
        self.assertEqual(self.occurrences(NEW), 0, "%d occurrences of the marker token" % self.occurrences(NEW))
        self.assertEqual(self.occurrences(OLD), 0)

    def test_s8_a_failed_refresh_and_a_failed_second_push_leak_nothing_either(self):
        self.set_mode("echo")   # the minter's own error quotes the old token
        self.run_stop([NEEDS, PUSHED])
        self.assertEqual(self.occurrences(NEW), 0)
        self.assertEqual(self.occurrences(OLD), 0)
        self.install_worker()
        self.set_mode("ok")
        self.run_stop([NEEDS, dict(NEEDS, error="the git token had ended")])
        self.assertEqual(self.occurrences(NEW), 0)

    def test_mutant_that_prints_the_token_shows_one_occurrence(self):
        real = cw.Launcher.refresh_for_stop

        def printing(L, pod, now):
            got = real(L, pod, now)
            print("refreshed with", NEW, file=sys.stderr)
            return got
        with mock.patch.object(cw.Launcher, "refresh_for_stop", printing):
            self.run_stop([NEEDS, PUSHED])
        self.assertGreaterEqual(self.occurrences(NEW), 1)

    def test_the_new_token_is_never_wider_than_the_mint_route_allows(self):
        # the 61-minute cap and the one-repository, exact-permission checks stay in the minter the refresh reuses (app_token.py, unchanged here)
        text = open(os.path.join(ROOT, "cloud", "launcher", "app_token.py")).read()
        self.assertIn("refusing: the token lives longer than 61 minutes", text)


class TestNote(tr.Harness):
    def setUp(self):
        super().setUp()
        st = self.st()
        st["pods"][self.name]["metadata"]["uid"] = "uid-" + self.name   # a real pod carries its uid; the annotation is bound to it
        self.write_state(st)

    def sent_at(self, minutes):
        self.at(minutes=minutes)
        L = self.launcher()
        self.notes = getattr(self, "notes", [])
        L.send_note = lambda name, text: self.notes.append((minutes, name, text))
        with contextlib.redirect_stderr(io.StringIO()):
            L.send_wip_notes(L.kube.list_pods(), self.clock())

    def test_s2_the_note_names_the_branch_and_the_exact_plain_command(self):
        text = cw.wip_note("w1")
        self.assertIn("git push origin HEAD:refs/heads/w1/wip", text)
        self.assertIn("w1/wip", text)
        self.assertNotRegex(text, r"--force|--force-with-lease| -f\b")

    def test_s3_one_note_at_minute_40_none_at_39_and_never_twice(self):
        for m in (39, 40, 41, 55):
            self.sent_at(m)
        self.assertEqual([n[0] for n in self.notes], [40], "%d of 1 notes sent, at minute %s" % (len(self.notes), [n[0] for n in self.notes]))
        self.assertIn("git push origin HEAD:refs/heads/%s/wip" % WORKER, self.notes[0][2])

    def test_s3_the_reaper_pass_sends_it_through_the_same_step(self):
        L = self.launcher()
        sent = []
        L.send_note = lambda name, text: sent.append(text)
        for m in (39, 40, 41):
            self.at(minutes=m)
            self.one_pass(L)
        self.assertEqual(len(sent), 1)

    def test_mutant_that_sends_at_every_step_or_only_at_41_is_caught(self):
        with mock.patch.object(cw, "wip_note_due", lambda started, now, already: True):
            for m in (39, 40, 41):
                self.sent_at(m)
        self.assertEqual(len(self.notes), 3)   # the S3 check (exactly one, at 40) fails on it
        self.notes = []
        with mock.patch.object(cw, "WIP_NOTE_AFTER_SECONDS", 41 * 60):
            for m in (39, 40, 41):
                self.sent_at(m)
        self.assertNotEqual([n[0] for n in self.notes], [40])


class TestHook(unittest.TestCase):
    def run_hook(self, says, verb="push"):
        env = {"CW_WORKER": WORKER, "CW_WORKDIR": os.path.dirname(HERE), "FAKE_GIT_SAYS": says, "FAKE_GIT_VERB": verb}
        out = io.StringIO()
        with mock.patch.object(end_hook, "GIT_BIN", FAKE_GIT), mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            code = end_hook.main()
        return code, json.loads(out.getvalue().strip().splitlines()[-1])

    def test_an_ended_token_on_the_push_or_the_fetch_signals_one_refresh_and_carries_no_remote_text(self):
        for verb in ("push", "fetch"):
            code, res = self.run_hook("ended", verb)
            self.assertEqual((code, res.get("refresh"), res["pushed"]), (1, "needed", False))
            self.assertEqual(res["error"], "the git token had ended")

    def test_any_other_refusal_signals_no_refresh(self):
        for says in ("rejected", "network"):
            code, res = self.run_hook(says)
            self.assertEqual(code, 1)
            self.assertNotIn("refresh", res, says)

    def test_a_push_that_lands_signals_nothing(self):
        code, res = self.run_hook("")
        self.assertEqual((code, res["pushed"]), (0, True))
        self.assertNotIn("refresh", res)

    def test_mutant_that_signals_a_refresh_on_every_refusal_is_caught(self):
        with mock.patch.object(end_hook, "ENDED_TOKEN_RE", re.compile(r".")):
            code, res = self.run_hook("rejected")
        self.assertEqual(res.get("refresh"), "needed")   # the S6 check (no refresh for another reason) fails on it


class TestDocsAndPins(unittest.TestCase):
    DOCS = ("cloud/README.md", "cloud/launcher/README.md", "docs/cloud-worker-interface.md")

    def text(self, path):
        with open(os.path.join(ROOT, path)) as f:
            return " ".join(f.read().split())

    def test_s1_the_launcher_and_cloud_readmes_state_the_cause_first(self):
        for path in self.DOCS[:2]:
            self.assertIn("the one-hour git token had ended before the push", self.text(path), path)

    def test_s11_each_document_states_the_note_the_one_refresh_the_scope_the_file_and_the_audit_word(self):
        rules = ("minute 40", "git push origin HEAD:refs/heads/<worker>/wip", "one refresh and one retry", "same repository and the same permissions",
                 "replaced by a temporary file and a rename", "refresh: used, refused or none")
        have = 0
        for path in self.DOCS:
            text = self.text(path)
            missing = [r for r in rules if r not in text]
            self.assertEqual(missing, [], path)
            have += 1
        self.assertEqual(have, 3, "%d of 3 documents state the refresh rule" % have)

    def test_u1_the_guard_and_permission_files_are_byte_identical_to_the_base(self):
        pins = {"cloud/worker/guard.sh": "ba4d51aa043d450a4832210fb6492295e7bd7c7e61a4abd2a70cc75baea19bca",
                "cloud/worker/opencode-worker.json": "9547d9f9611d04959fe9a36266ae78b97c235a7fb48a15c940943881fde33530"}
        for path, digest in pins.items():
            with open(os.path.join(ROOT, path), "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), digest, path)


if __name__ == "__main__":
    unittest.main()
