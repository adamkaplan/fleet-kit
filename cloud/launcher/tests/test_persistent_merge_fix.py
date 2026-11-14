"""Ask #1509, the merge fixes (plan 774ab33b): S18 (the refusal table row of the persistent flag without cloud), S19 (the reaper's renewal and the
wip note know a persistent pod), S20 (the cost figure follows the orchestrator row) and S21 (the created-words entries; part two, the two refusals
create nothing, is further down). Every printed count is computed and asserted; each mutant is counted as applied and its control runs first.
Offline; the temp trees live outside the repository and are removed."""
import datetime as dt
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "worker", "tests"))
import stub_support  # noqa: E402
import test_persistent_orchestrator as po  # noqa: E402

cw, mutant, ROOT = po.cw, po.mutant, po.ROOT
BIN_SWITCHBOARD_TEST = os.path.join(ROOT, "bin", "test-switchboard")
ROW_LINE = '''            ' > getattr(args, "persistent", Fa|return EXIT_USAGE': (B + ["--persistent"], []),'''   # the row alone: another row now follows it on the same line (#2365)
RESULT = "refused before any pod exists: it says nothing was created"
ENTRY_LINES = ["        '--persistent is only for --age': '%s',\n" % RESULT, "        '--persistent does not combine ': '%s',\n" % RESULT]
INTEGRATION_KEYS = ["'the stack model secret %s coul'", "'%s; nothing was created'"]   # what the scan names for the raise integration added (#1435): the text is cut at 30 characters, and its second form is the bare tail
DERIVED = "test_s6_every_refusal_exit_derived_from_the_source_has_a_row_and_sends_exactly_one_message"
CREATED = "test_s9_no_raise_of_the_launcher_mentions_a_created_worker_in_other_words"
BLOCK_RENEWAL = '''            if ann(pod, "persistent") is not None:   # a persistent pod has no hard deadline: it is always before its limit
                if self._persistent_owned(pod):   # only a pod whose own workload is read and owns it is renewed; the reap pass reports the others
                    out.append(pod)
                continue
'''
BLOCK_NOTE = '''            if ann(pod, "persistent") is not None:   # an always-on orchestrator is not asked to push at minute 40
                continue
'''
FORBIDDEN = 'Error from server (Forbidden): statefulsets.apps "%s" is forbidden: User "cw-operator" cannot get resource "statefulsets" in API group "apps" in the namespace "cw"\n' % po.NAME


class CopyRun(unittest.TestCase):
    """A copy of bin/test-switchboard in a temp tree whose other entries are links to the repository's: it runs ONE test of the class."""

    def run_copy(self, text, k):
        tmp = tempfile.mkdtemp(prefix="cw-merge-fix-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "bin"))
        for entry in os.listdir(ROOT):
            if entry not in ("bin", ".git"):
                os.symlink(os.path.join(ROOT, entry), os.path.join(tmp, entry))
        for entry in os.listdir(os.path.join(ROOT, "bin")):
            if entry != "test-switchboard":
                os.symlink(os.path.join(ROOT, "bin", entry), os.path.join(tmp, "bin", entry))
        target = os.path.join(tmp, "bin", "test-switchboard")
        load = "    suite = loader.loadTestsFromModule(sys.modules[__name__])\n"
        self.assertEqual(text.count(load), 1)
        with open(target, "w") as f:   # only the one class these checks run is turned into test cases (the other ~1,000 classes are defined, not instantiated)
            f.write(text.replace(load, "    suite = unittest.TestSuite([loader.loadTestsFromTestCase(TestLaunchRefusedMessage)])\n"))
        os.chmod(target, 0o755)
        p = stub_support.start(self, [sys.executable, target, "-k", "TestLaunchRefusedMessage." + k], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out, _ = p.communicate(timeout=90)
        return p.returncode, out

    def run_both(self, control, mutant, k):
        """The control and the mutant copy side by side (each loads the whole file): (control result, mutant result)."""
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            first, second = pool.submit(self.run_copy, control, k), pool.submit(self.run_copy, mutant, k)
            return first.result(), second.result()

    def source(self):
        with open(BIN_SWITCHBOARD_TEST) as f:
            return f.read()


class S18Row(CopyRun):
    def test_s18_the_control_with_the_row_passes_and_the_table_without_it_fails_with_the_anchored_text(self):
        src = self.source()
        self.assertEqual(src.count(ROW_LINE), 1)
        mutated = src.replace(ROW_LINE, "")
        self.assertEqual(len(src) - len(mutated), len(ROW_LINE))   # the mutation applied, counted
        (code, out), (mcode, mout) = self.run_both(src, mutated, DERIVED)
        self.assertEqual(code, 0, out[-600:])   # the control: 1 of 1
        code, out = mcode, mout
        self.assertNotEqual(code, 0)
        self.assertIn("a derived exit has no row, or a row no exit", out)


class S21Entries(CopyRun):
    def with_integration_entries(self, src):
        """The copy's exempt table also names every other pre-creation raise of the launcher that integration added (the stack model secret
        read failure, #1435): a key the table lacks is added to the copy only, so the control isolates the two persistent entries."""
        head = "    CREATED_EXEMPT = {\n"
        self.assertEqual(src.count(head), 1)
        added = "".join("        %s: '%s',\n" % (key, RESULT) for key in INTEGRATION_KEYS if key not in src)
        src = src.replace(head, head + added)
        # The scan now also passes a raise whose own words say "nothing was created" (so the two entries are no longer what keeps it quiet):
        # the copy switches that rule off, so the table entries are again the only thing that exempts the two persistent raises.
        rule = '            if re.search(r"nothing was created", text):'
        self.assertEqual(src.count(rule), 1)
        return src.replace(rule, "            if False:")

    def test_s21_the_control_passes_and_the_copy_without_the_two_entries_names_the_stray_raise(self):
        src = self.source()
        for line in ENTRY_LINES:
            self.assertEqual(src.count(line), 1)
        src = self.with_integration_entries(src)
        mutated = src
        for line in ENTRY_LINES:
            mutated = mutated.replace(line, "")
        self.assertEqual(len(src) - len(mutated), sum(len(x) for x in ENTRY_LINES))   # both mutations applied
        (code, out), (mcode, mout) = self.run_both(src, mutated, CREATED)
        self.assertEqual(code, 0, out[-600:])   # the control: 1 of 1
        code, out = mcode, mout
        self.assertNotEqual(code, 0)
        self.assertIn("'--persistent is only for --age'", out)
        self.assertIn("'--persistent does not combine '", out)


class S19Reaper(po.PBase):
    """The two steps of the reaper that read a deadline, on a persistent pod and on a one-shot pod."""

    def pod(self, name):
        return self.st()["pods"][name]

    def launcher(self):
        return self.mod.Launcher(self.mod.Kube(self.env), self.env, self.clock)

    def check_renewal(self):
        """owned persistent pod seen 1 of 1; its workload unreadable seen 0 of 1; one-shot pod seen before its deadline and not after."""
        self.launch()
        pod = self.pod(po.POD)
        self.assertIsNone(cw.ann(pod, "hard-deadline"))
        seen = self.launcher()._before_hard_limit([pod], self.clock())
        self.assertEqual([p["metadata"]["name"] for p in seen], [po.POD], "the owned persistent pod was not renewed")   # renewed 1 of 1
        self.patch_state(sts_get_stderr=FORBIDDEN)
        self.assertEqual(self.launcher()._before_hard_limit([pod], self.clock()), [])   # unreadable: renewed 0 of 1
        self.patch_state(sts_get_stderr="")
        self.create("one1")
        one = self.pod(cw.pod_name("one1"))
        self.assertEqual(len(self.launcher()._before_hard_limit([one], self.clock())), 1)
        self.clock.advance(hours=3)
        self.assertEqual(self.launcher()._before_hard_limit([one], self.clock()), [])

    def check_notes(self):
        """no note and no write for a persistent pod at minute 41; one note for a one-shot pod."""
        self.launch()
        self.create("one1")
        for name in (po.POD, cw.pod_name("one1")):
            st = self.st()
            st["pods"][name].setdefault("status", {})["phase"] = "Running"
            self.write_state(st)
        self.clock.advance(minutes=41)
        open(self.env["FAKE_KUBE_CALLS"], "w").close()
        self.launcher().send_wip_notes([self.pod(po.POD)], self.clock())
        self.assertEqual(self.writes(), [], "a note or a patch was written to the persistent pod")
        self.assertEqual([v for v in self.verbs() if v[0] == "exec"], [], "a note was sent to the persistent pod")
        self.launcher().send_wip_notes([self.pod(cw.pod_name("one1"))], self.clock())
        self.assertEqual(len([v for v in self.verbs() if v[0] == "exec"]), 1)   # the one-shot pod gets its note

    def test_s19_the_renewal_sees_an_owned_persistent_pod_and_not_an_unreadable_one(self):
        self.check_renewal()

    def test_s19_a_persistent_pod_gets_no_wip_note_and_a_worker_pod_does(self):
        self.check_notes()

    def test_s19_the_copy_without_the_renewal_block_fails_the_renewal_check(self):   # its control is the test above (the unmutated file passes)
        self.mod = mutant(BLOCK_RENEWAL, "")   # applied once (counted by mutant)
        with self.assertRaises(AssertionError) as caught:
            self.check_renewal()
        self.assertIn("the owned persistent pod was not renewed", str(caught.exception))

    def test_s19_the_copy_without_the_note_block_fails_the_no_note_check(self):   # its control is the test above (the unmutated file passes)
        self.mod = mutant(BLOCK_NOTE, "")
        with self.assertRaises(AssertionError) as caught:
            self.check_notes()
        self.assertIn("a note or a patch was written to the persistent pod", str(caught.exception))


class S20Docs(unittest.TestCase):
    DOCS = {"docs/cloud-operator-quickstart.md": ("(`O`, USD 0.02 an hour) that is USD 0.48 a day", "USD 0.48 a day"),
            "cloud/launcher/README.md": ("default orchestrator shape `O`: USD 0.48 a day", "USD 0.48 a day")}

    def read(self, rel):
        with open(os.path.join(ROOT, rel)) as f:
            return " ".join(f.read().split())

    def check(self, texts):
        rate = cw.SHAPES[cw.DEFAULT_ORCHESTRATOR_SHAPE]["usd_per_hour"]
        self.assertEqual(rate, 0.02)
        figure = "USD %.2f a day" % (24 * rate)
        self.assertEqual(figure, "USD 0.48 a day")
        for rel, (sentence, _) in self.DOCS.items():
            self.assertIn(sentence, texts[rel], rel)
            self.assertIn(figure, texts[rel], rel)
        found = re.search(r"USD (\d+\.\d\d) an hour\) that is USD (\d+\.\d\d) a day", texts["docs/cloud-operator-quickstart.md"])
        self.assertAlmostEqual(float(found.group(2)), 24 * float(found.group(1)), places=2)
        return len(self.DOCS)

    def test_s20_both_documents_carry_the_figure_of_24_times_the_row(self):
        self.assertEqual(self.check({rel: self.read(rel) for rel in self.DOCS}), 2)

    def test_s20_a_copy_that_keeps_the_old_figure_fails_and_the_control_passes(self):
        texts = {rel: self.read(rel) for rel in self.DOCS}
        self.check(texts)   # the control
        old = "(`O`, USD 0.02 an hour) that is USD 0.48 a day"
        self.assertEqual(texts["docs/cloud-operator-quickstart.md"].count(old), 1)
        texts["docs/cloud-operator-quickstart.md"] = texts["docs/cloud-operator-quickstart.md"].replace(old, "(`S`, USD 0.10 an hour) that is USD 2.40 a day")
        with self.assertRaises(AssertionError):
            self.check(texts)


REFUSAL_CALL = "                self._persistent_refusals(a, agent, run_values)\n                max_persistent(self.env)"
FIRST_CREATE = "                if run_values:\n                    self._make(made, \"run\", self.kube.create_run_secret"


class S21RefusalsCreateNothing(po.PBase):
    """S21 part two: each of the two exempt raises happens before the first create. The mutant moves the refusal call behind the first create
    (the password Secret's), where the launcher's own cleanup would hide the objects: the RECORDED calls show them."""
    CASES = {"a worker with --persistent": (dict(agent=False), (), "--persistent is only for --agent orchestrator"),
             "an orchestrator with --persistent --stack": (dict(), ("--stack", "--shape", "S"), "--persistent does not combine with --stack")}

    def check_refusal(self, case):
        kw, extra, text = self.CASES[case]
        code, out, err = self.launch(po.WORKER, *extra, expect=2, **kw)
        self.assertEqual(code, 2)
        self.assertEqual(err.count(text), 1, err)   # the launcher's refusal line, the flag text exactly once
        creates = [v for v in self.writes() if v[0] in ("create", "apply")]
        self.assertEqual(creates, [], "something was created before the refusal: %r" % (creates,))   # 0 creates in the recorded calls
        self.created_nothing()   # no StatefulSet, no Pod, no per-run Secret, no hub entry

    def test_s21_each_exempt_refusal_exits_2_with_one_line_and_creates_nothing(self):
        for case in self.CASES:
            with self.subTest(case):
                self.check_refusal(case)
                self.patch_state(secrets={po.REGISTRY: 1, "cloud-hub-pw": 1})
                open(self.env["FAKE_KUBE_CALLS"], "w").close()

    def test_s21_the_launcher_with_the_refusal_behind_the_first_create_fails_each_with_the_anchored_text(self):
        with open(po.SOURCE) as f:
            src = f.read()
        self.assertEqual(src.count(REFUSAL_CALL), 1)
        self.assertEqual(src.count(FIRST_CREATE), 1)
        moved = src.replace(REFUSAL_CALL, "                max_persistent(self.env)").replace(FIRST_CREATE, "                self._persistent_refusals(a, agent, run_values)\n" + FIRST_CREATE)
        self.assertNotEqual(moved, src)   # the mutation applied: moved, not removed
        self.assertEqual(moved.count("self._persistent_refusals(a, agent, run_values)"), 1)
        guard = "    bad = workload_problems(workload)\n"   # the stack case is also stopped by the workload's own field guard before any create: the mutant relaxes it (counted) so the ORDER shows
        self.assertEqual(moved.count(guard), 1)
        moved = moved.replace(guard, "    bad = []\n")
        self.mod = types_module(moved)
        for case in self.CASES:
            with self.subTest(case):
                with self.assertRaises(AssertionError) as caught:
                    self.check_refusal(case)
                self.assertIn("something was created before the refusal", str(caught.exception))
                self.patch_state(secrets={po.REGISTRY: 1, "cloud-hub-pw": 1})
                open(self.env["FAKE_KUBE_CALLS"], "w").close()


RETRY_CALL = "                        res = self.run_hook(target)\n"
NEEDS_REFRESH = {"pushed": False, "wip_branch": None, "error": "the git token had ended", "refresh": "needed"}


class S22RetryTarget(po.PBase):
    """S22: the stop's retry after a refreshed token runs the end hook on the same pod as the first call (the persistent pod NAME-0)."""
    printed = {}

    def hook_targets(self, worker, *, persistent, fresh=True):
        import argparse
        import contextlib
        import io
        if not fresh:
            pass   # the same world again: the failed hook kept the pod and its workload
        elif persistent:
            self.launch()
        else:
            self.create(worker)
        targets = []
        L = self.mod.Launcher(self.mod.Kube(self.env), self.env, self.clock)

        def hook(target):
            targets.append(target)
            return dict(NEEDS_REFRESH)
        L.run_hook = hook
        L.refresh_for_stop = lambda pod, now: self.mod.REFRESH_USED
        a = argparse.Namespace(worker=worker, reason="done", run_id=None, no_record=True, record_dir=None)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                L.stop(a)
            except self.mod.CloudError:
                pass   # the scripted answer keeps the pod: the stop reports the hook failure; only the targets matter
        return targets

    def check_persistent(self, fresh=True):
        targets = self.hook_targets(po.WORKER, persistent=True, fresh=fresh)
        self.assertEqual(len(targets), 2, targets)
        self.assertEqual(targets, [po.POD, po.POD], "the retry acted on a different pod than the first call: %r" % (targets,))
        self.assertNotIn(po.NAME, targets)
        return targets

    def test_s22_the_retry_acts_on_the_persistent_pod_like_the_first_call(self):
        S22RetryTarget.printed["p"] = self.check_persistent()

    def test_s22_a_one_shot_stop_runs_both_hooks_on_its_own_pod_name(self):
        targets = self.hook_targets("one1", persistent=False)
        self.assertEqual(targets, [cw.pod_name("one1")] * 2)
        S22RetryTarget.printed["o"] = targets

    def test_s22_the_copy_whose_retry_acts_on_the_name_fails_with_the_anchored_text_and_the_control_passes(self):
        control = self.check_persistent()   # CONTROL FIRST: the unmutated file passes the same check
        self.assertEqual(control, [po.POD, po.POD])
        self.mod = mutant(RETRY_CALL, "                        res = self.run_hook(name)\n")   # counted once and applied by mutant
        with self.assertRaises(AssertionError) as caught:
            self.check_persistent(fresh=False)
        self.assertIn("the retry acted on a different pod than the first call", str(caught.exception))
        p, o = S22RetryTarget.printed.get("p", control), S22RetryTarget.printed.get("o", [cw.pod_name("one1")] * 2)
        print("S22: persistent retry hook targets %s and %s, one-shot targets %s, control passes 1 of 1, mutant fails 1 of 1 with the anchored text, "
              "mutation applied True" % (p[:1], p[1:], o))


def types_module(changed):
    import types
    mod = types.ModuleType("cloud_worker_moved")
    mod.__file__ = po.SOURCE
    exec(compile(changed, po.SOURCE, "exec"), mod.__dict__)
    return mod


if __name__ == "__main__":
    unittest.main()
