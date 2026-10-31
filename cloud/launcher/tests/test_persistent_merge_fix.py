"""Ask #1509, merge fixes PC-8, PC-9, PC-10 (scenarios S18, S19, S20): the reaper's renewal and wip note know a persistent pod, the persistent cost
figure follows the orchestrator row, and the refusal table has a row for the persistent flag without --cloud. Each check has a mutant (a computed
text change, asserted applied) that must fail it and a control (the unmutated text) that must pass. Nothing is written under the repository."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
import cloud_worker as cw  # noqa: E402
import test_persistent_orchestrator as po  # noqa: E402
import test_git_token_renewal as renewal  # noqa: E402

SOURCE = os.path.join(LAUNCHER, "cloud_worker.py")
BLOCK_RENEW = ('            if ann(pod, "persistent") is not None:   # a persistent pod has no hard deadline: it is always before its limit\n'
               '                if self._persistent_owned(pod):   # only a pod whose own workload is read and owns it is renewed; the reap pass reports the others\n'
               '                    out.append(pod)\n                continue\n')
BLOCK_NOTE = ('            if ann(pod, "persistent") is not None:   # an always-on orchestrator is not asked to push at minute 40\n'
              '                continue\n')
FORBIDDEN = 'Error from server (Forbidden): statefulsets.apps "%s" is forbidden: User "cw-operator" cannot get resource "statefulsets" in API group "apps" in the namespace "cw"\n'


def read(path):
    with open(path) as f:
        return f.read()


def module_from(text):
    mod = types.ModuleType("cloud_worker_copy")
    mod.__file__ = SOURCE
    exec(compile(text, SOURCE, "exec"), mod.__dict__)
    return mod


def without_blocks():
    """The real source with the two inserted blocks removed (each asserted to occur exactly once). Returns (module, number of mutations applied)."""
    text, n = read(SOURCE), 0
    for block in (BLOCK_RENEW, BLOCK_NOTE):
        assert text.count(block) == 1, "the block to remove is not there exactly once"
        text, n = text.replace(block, ""), n + 1
    return module_from(text), n


class Rig:
    """The persistent-orchestrator renewal harness (fake cluster, fake mint, fake clock), borrowed whole, used outside a test method."""

    def __init__(self, mod):
        self.h = po.S6Renewal("test_s6_the_secret_is_found_by_the_workers_name_and_renewed")
        self.h.setUp()
        self.h.cwm = mod

    def close(self):
        self.h.doCleanups()

    def notes_and_writes(self, minute):
        """The wip-note step at the given minute of the pod's life: (notes sent, patch/annotate calls made)."""
        h = self.h
        h.at(minutes=minute)
        st = h.st()
        for pod in st["pods"].values():
            pod["metadata"].setdefault("uid", "uid-" + pod["metadata"]["name"])   # the note binds to the pod's uid
        h.write_state(st)
        open(h.env["FAKE_KUBE_CALLS"], "w").close()
        L, sent = h.launcher(), []
        L.send_note = lambda name, text: sent.append(name)
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            L.send_wip_notes(L.kube.list_pods(), L.clock())
        writes = [v for v in h.verbs_of_calls() if v[0] in ("patch", "annotate")]
        return len(sent), len(writes)

    def renewal(self, persistent, unreadable=False):
        """(renewed, hard_deadline calls on the pod) for the renewal step at minute 31."""
        h = self.h
        (h.install_persistent if persistent else h.install_worker)()
        if unreadable:
            h.patch_state(sts_get_stderr=FORBIDDEN % h.name)
        h.at(minutes=31)
        L, seen = h.launcher(), []
        real = L.hard_deadline
        L.hard_deadline = lambda pod: (seen.append(pod["metadata"]["name"]), real(pod))[1]
        pods = L.kube.list_pods()
        res = L.renew_git_tokens(L._before_hard_limit(pods, L.clock()), L.clock())
        return res == [(renewal.WORKER, "git token renewed", 0)], len(seen)


def _verbs_of_calls(self):
    import json
    out = []
    path = self.env["FAKE_KUBE_CALLS"]
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                if line.strip():
                    a = json.loads(line)
                    rest = a[a.index("-n") + 2:]
                    out.append((rest[0], rest[1] if len(rest) > 1 else "", rest[2] if len(rest) > 2 else ""))
    return out


po.S6Renewal.verbs_of_calls = _verbs_of_calls


def s19_checks(mod):
    """Facts about a cloud_worker module: owned persistent pod renewed, unreadable one not renewed, persistent notes 0, worker renewed, worker noted."""
    r = Rig(mod)
    try:
        renewed, calls = r.renewal(True)
        notes, writes = r.notes_and_writes(40)
        p_renew = renewed and calls == 0
        p_note = notes == 0 and writes == 0
    finally:
        r.close()
    r = Rig(mod)
    try:
        u_renewed, _ = r.renewal(True, unreadable=True)
        u_notes, u_writes = r.notes_and_writes(40)
    finally:
        r.close()
    r = Rig(mod)
    try:
        w_renew, _ = r.renewal(False)
        w_notes, _ = r.notes_and_writes(40)
    finally:
        r.close()
    return p_renew, p_note, w_renew, w_notes == 1, not u_renewed, u_notes == 0 and u_writes == 0


class S19Reaper(unittest.TestCase):
    def test_s19_persistent_pod_renewed_and_not_noted_worker_unchanged_mutant_fails_control_passes(self):
        p_renew, p_note, w_renew, w_note, u_not, u_nonote = s19_checks(cw)
        self.assertTrue(p_renew, "the owned persistent pod was not renewed, or hard_deadline was called on it")
        self.assertTrue(u_not, "a persistent pod with an unreadable workload was renewed")
        self.assertTrue(p_note and u_nonote, "a wip note or patch was written to a persistent pod")
        self.assertTrue(w_renew and w_note, "a worker pod behaves differently")
        control = sum([p_renew and p_note and u_not and u_nonote, w_renew and w_note])
        mod, n = without_blocks()
        m = s19_checks(mod)
        failed = [not m[0], not m[1]]
        self.assertEqual(sum(failed), 2, "the copy without the blocks must fail the owned renewal and the no-note check: %s" % failed)
        self.assertTrue(m[2] and m[3], "the worker behaviour must not depend on the blocks")
        self.assertEqual(n, 2)
        line = ("S19: owned persistent pod renewed %d of 1, persistent pod with an unreadable workload renewed %d of 1, wip notes written to either %d of 2, "
                "worker pods unchanged %d of 2, copy without the blocks fails %d of 2 checks, control passes %d of 2, mutations applied %s") % (
            int(p_renew), int(not u_not), int(not p_note) + int(not u_nonote), int(w_renew) + int(w_note), sum(failed), control, n)
        print(line)


class S20Figure(unittest.TestCase):
    def check(self, quick, readme):
        row = cw.SHAPES[cw.DEFAULT_ORCHESTRATOR_SHAPE]["usd_per_hour"]
        figure = "%.2f" % (24 * row)
        found = [("USD %s a day" % figure) in quick, "USD 0.02 an hour" in quick, ("USD %s a day" % figure) in readme]
        return all(found) and figure == "0.48", figure

    def test_s20_both_documents_carry_the_figure_the_old_figure_fails_control_passes(self):
        quick, readme = read(os.path.join(ROOT, "docs", "cloud-operator-quickstart.md")), read(os.path.join(LAUNCHER, "README.md"))
        ok, figure = self.check(quick, readme)
        self.assertTrue(ok)
        self.assertEqual(quick.count("USD 0.48 a day") >= 1 and readme.count("USD 0.48 a day") >= 1, True)
        self.assertEqual(quick.count("USD 0.48 a day"), 1)
        old_q, old_r = quick.replace("USD 0.48 a day", "USD 2.40 a day"), readme.replace("USD 0.48 a day", "USD 2.40 a day")
        self.assertNotEqual(old_q, quick)
        self.assertNotEqual(old_r, readme)
        mutant_ok, _ = self.check(old_q, old_r)
        self.assertFalse(mutant_ok)
        print("S20: 2 of 2 documents carry the figure %s (24 times the row), copy with the old figure fails %d of 1, control passes %d of 1" % (figure, int(not mutant_ok), int(ok)))


ROW = '''            ' > getattr(args, "persistent", Fa|return EXIT_USAGE': (B + ["--persistent"], []),\n'''
TEST = "TestLaunchRefusedMessage.test_s6_every_refusal_exit_derived_from_the_source_has_a_row_and_sends_exactly_one_message"


def run_child(tree):
    p = subprocess.run([sys.executable, os.path.join(tree, "bin", "test-switchboard"), "-k", TEST], cwd=tree, capture_output=True, text=True, timeout=90,
                       env={k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR")})
    return p.returncode, p.stdout + p.stderr


class S18Table(unittest.TestCase):
    def test_s18_the_class_passes_with_the_row_and_fails_without_it_with_the_anchored_text(self):
        if shutil.which("git") is None or not os.path.exists(os.path.join(ROOT, "bin", "test-switchboard")):
            self.skipTest("SKIPPED LOUDLY: no git or no bin/test-switchboard here")
        tmp = tempfile.mkdtemp(prefix="cw-s18-")
        try:
            tree = os.path.join(tmp, "tree")
            shutil.copytree(ROOT, tree, symlinks=True, ignore=shutil.ignore_patterns(".git"))
            code, out = run_child(tree)
            self.assertEqual(code, 0, out[-600:])
            self.assertRegex(out, r"1 run, 1 passed")
            path = os.path.join(tree, "bin", "test-switchboard")
            text = read(path)
            self.assertEqual(text.count(ROW), 1)
            mutated = text.replace(ROW, "")
            self.assertNotEqual(mutated, text)
            with open(path, "w") as f:
                f.write(mutated)
            mcode, mout = run_child(tree)
            self.assertNotEqual(mcode, 0)
            self.assertIn("a derived exit has no row", mout)
            print("S18: class passes %d of 1, the table without the row fails %d of 1 with the anchored text, control passes %d of 1, mutation applied %s"
                  % (int(code == 0), int(mcode != 0 and "a derived exit has no row" in mout), int(code == 0), text != mutated))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


E1 = "        '--persistent is only for --age': 'refused before any pod exists: it says nothing was created',\n"
E2 = "        '--persistent does not combine ': 'refused before any pod exists: it says nothing was created',\n"
T21 = "TestLaunchRefusedMessage.test_s9_no_raise_of_the_launcher_mentions_a_created_worker_in_other_words"


def run_child21(tree, key):
    p = subprocess.run([sys.executable, os.path.join(tree, "bin", "test-switchboard"), "-k", key], cwd=tree, capture_output=True, text=True, timeout=90,
                       env={k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR")})
    return p.returncode, p.stdout + p.stderr


class S21Exempt(unittest.TestCase):
    def test_s21_the_class_passes_with_the_entries_and_fails_without_them_naming_the_stray(self):
        if not os.path.exists(os.path.join(ROOT, "bin", "test-switchboard")):
            self.skipTest("SKIPPED LOUDLY: no bin/test-switchboard here")
        tmp = tempfile.mkdtemp(prefix="cw-s21-")
        try:
            tree = os.path.join(tmp, "tree")
            shutil.copytree(ROOT, tree, symlinks=True, ignore=shutil.ignore_patterns(".git"))
            code, out = run_child21(tree, T21.split(".")[1])
            self.assertEqual(code, 0, out[-600:])
            self.assertRegex(out, r"1 run, 1 passed")
            path = os.path.join(tree, "bin", "test-switchboard")
            text = read(path)
            n = 0
            for e in (E1, E2):
                self.assertEqual(text.count(e), 1)
                text, n = text.replace(e, ""), n + 1
            with open(path, "w") as f:
                f.write(text)
            mcode, mout = run_child21(tree, T21.split(".")[1])
            self.assertNotEqual(mcode, 0)
            self.assertIn("--persistent is only for --age", mout)
            self.assertIn("--persistent does not combine ", mout)
            strays = 2
            print("S21: class passes %d of 1 test, copy without the entries fails %d of 1 naming %d stray raises, control passes %d of 1, mutation applied %s"
                  % (int(code == 0), int(mcode != 0), strays, int(code == 0), n == 2))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
