"""A pod with no repository checkout is stopped and deleted (ask #1483).

Three cases the end hook and the stop tell apart:
  (a) /work/repo is absent, or exists with no .git: nothing was cloned, nothing can be lost; the stop proceeds, the pod is deleted,
      the record's end_hook note and the audit reason say `no repository in the pod; nothing to push`;
  (b) a checkout whose push fails (rejected push, expired token, conflict): the pod is KEPT and the stop says why;
  (c) nothing unpushed or a good push: as before (covered by test_cloud_worker.py).
Offline: the fake kubectl; a mutant is the launcher's hook reader patched in memory, never a file."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw

SENTENCE = "no repository in the pod; nothing to push"
FAILURES = {"coder1": "remote rejected the push: protected branch", "coder2": "authentication failed: the token has expired",
            "coder3": "push failed: non-fast-forward, conflict with the remote branch"}
DOC = os.path.join(ROOT, "docs", "cloud-worker-interface.md")


class StopNoCheckout(base.Base):
    def stop_pod(self, worker, hook_env=None, hook_result=None):
        """Create a pod, set what its end hook sees, stop it; returns (code, err, record dict or None)."""
        self.create(worker)
        kw = {}
        if hook_env is not None:
            kw["hook_env"] = dict(hook_env, CW_WORKER=worker)
        if hook_result is not None:
            kw["hook_result"] = hook_result
        self.patch_state(**kw)
        rec = os.path.join(self.tmp.name, "rec-" + worker)
        code, out, err = self.run_cli("stop", worker, "--record-dir", rec)
        runs = os.listdir(rec) if os.path.isdir(rec) else []
        record = None
        if runs:
            with open(os.path.join(rec, runs[0], "run.json")) as f:
                record = json.load(f)
        return code, err, record

    def stop_audit(self, worker):
        return [x for x in self.audit_lines() if x["action"] == "stop" and x["worker"] == worker][-1]

    # ---- S2: no checkout ----
    def problems_no_checkout(self):
        """Why the no-checkout behaviour is NOT as asked; an empty list is as asked."""
        absent = os.path.join(self.tmp.name, "absent", "repo")
        bare_dir = os.path.join(self.tmp.name, "plain")
        os.makedirs(bare_dir)
        bad = []
        for worker, path in (("coder4", absent), ("coder5", bare_dir)):
            code, err, record = self.stop_pod(worker, {"CW_WORKDIR": path})
            if code != 0:
                bad.append("%s: stop exit %s" % (worker, code))
            if self.pod(worker) is not None:
                bad.append("%s: pod not deleted" % worker)
            if not record or record["end_hook"].get("note") != SENTENCE:
                bad.append("%s: record note %r" % (worker, (record or {}).get("end_hook", {}).get("note")))
            if SENTENCE not in self.stop_audit(worker)["reason"]:
                bad.append("%s: audit reason lacks the sentence" % worker)
        return bad

    def test_a_pod_with_no_directory_or_no_git_dir_is_deleted_with_the_sentence(self):
        self.assertEqual(self.problems_no_checkout(), [])

    def test_the_audit_and_record_for_no_checkout_are_exact(self):
        code, err, record = self.stop_pod("coder4", {"CW_WORKDIR": os.path.join(self.tmp.name, "nowhere", "repo")})
        self.assertEqual(code, 0, err)
        self.assertEqual(record["end_hook"]["note"], SENTENCE)
        self.assertIs(record["end_hook"]["pushed"], False)
        self.assertEqual(record["end_hook"]["error"], "")
        self.assertTrue(self.stop_audit("coder4")["ok"])
        self.assertIn(SENTENCE, err)

    def test_the_hook_tells_the_three_cases_apart(self):
        import end_hook
        missing = end_hook.run(os.path.join(self.tmp.name, "none", "repo"), "coder7")
        self.assertIs(missing["no_repository"], True)
        self.assertEqual(missing["note"], SENTENCE)
        work = self.checkout_without_remote()
        with self.assertRaises(RuntimeError):   # a checkout whose push path fails raises: the caller prints `error` and exits 1
            end_hook.run(work, "coder7")

    # ---- S3: a failed push keeps the pod ----
    def problems_failed_push(self):
        bad = []
        for worker, why in FAILURES.items():
            code, err, record = self.stop_pod(worker, hook_result={"pushed": False, "wip_branch": None, "error": why})
            if code != cw.EXIT_HOOK:
                bad.append("%s: stop exit %s" % (worker, code))
            if self.pod(worker) is None:
                bad.append("%s: pod deleted" % worker)
            if why not in err:
                bad.append("%s: reason not said" % worker)
            if not record or record["end_hook"].get("error") != why:
                bad.append("%s: record has no error" % worker)
        return bad

    def test_three_failed_pushes_keep_all_three_pods_with_a_reason(self):
        self.assertEqual(self.problems_failed_push(), [])
        self.assertEqual(sum(1 for w in FAILURES if self.pod(w) is not None), 3)

    def checkout_without_remote(self):
        import subprocess
        work = os.path.join(self.tmp.name, "co")
        subprocess.run(["git", "init", "-q", work], check=True)
        subprocess.run(["git", "-C", work, "remote", "add", "origin", os.path.join(self.tmp.name, "gone.git")], check=True)
        return work

    def test_a_real_checkout_with_an_unreachable_remote_keeps_the_pod(self):
        work = self.checkout_without_remote()
        code, err, record = self.stop_pod("coder6", {"CW_WORKDIR": work})
        self.assertEqual(code, cw.EXIT_HOOK, err)
        self.assertIsNotNone(self.pod("coder6"))
        self.assertTrue(record["end_hook"]["error"])
        self.assertNotIn(SENTENCE, json.dumps(record["end_hook"]))

    # ---- S4: mutants ----
    def mutate(self, edit):
        """Patch the launcher's hook reader so that edit(result) changes what it sees; returns the call counter (mutation applied)."""
        orig = cw.Launcher.run_hook
        applied = []

        def patched(launcher, name):
            res = orig(launcher, name)
            if edit(res):
                applied.append(1)
            return res
        cw.Launcher.run_hook = patched
        self.addCleanup(setattr, cw.Launcher, "run_hook", orig)
        return applied

    def test_mutant_keeping_the_pod_for_an_absent_directory_fails_the_no_checkout_checks(self):
        def edit(res):
            if res.get("no_repository"):   # the old behaviour: no checkout counts as a failure
                res.pop("no_repository")
                res["error"] = "fatal: cannot change to /work/repo: No such file or directory"
                return True
            return False
        applied = self.mutate(edit)
        self.assertNotEqual(self.problems_no_checkout(), [])
        self.assertTrue(applied)

    def test_mutant_deleting_the_pod_for_a_failed_push_fails_the_failed_push_checks(self):
        def edit(res):
            if res.get("error"):   # a failed push no longer reaches the stop as a failure
                res.pop("error")
                return True
            return False
        applied = self.mutate(edit)
        self.assertNotEqual(self.problems_failed_push(), [])
        self.assertTrue(applied)

    # ---- S5: docs ----
    @staticmethod
    def says_both(text):
        text = " ".join(text.split())
        return all(p in text for p in ("A pod with no checkout", "the stop goes on and deletes the pod", SENTENCE,
                                       "A failed push in a real checkout", "it keeps the pod"))

    def test_the_stop_documentation_says_both_cases(self):
        with open(DOC, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(self.says_both(text))
        self.assertFalse(self.says_both(text.replace("the stop goes on and deletes the pod", "")))   # a mutant copy, in memory
        self.assertFalse(self.says_both(text.replace("it keeps the pod", "")))


    # The hook reports no repository when it has looked: an unreadable state or a checkout elsewhere keeps the pod (ask #1483).
    def setUp(self):
        super().setUp()
        self.modes = []
        self.addCleanup(self.restore_modes)
        self.space = os.path.join(self.tmp.name, "space")
        os.makedirs(self.space)

    def restore_modes(self):
        for p, m in reversed(self.modes):
            try:
                os.chmod(p, m)
            except OSError:
                pass

    def set_mode(self, path, mode):
        self.modes.append((path, os.stat(path).st_mode & 0o7777))
        os.chmod(path, mode)

    def need_user(self):
        if os.geteuid() == 0:
            self.skipTest("running as root: root ignores file modes, so the permission states cannot be made")

    def repo_dir(self, git_marker=True):
        d = os.path.join(self.space, "repo")
        os.makedirs(os.path.join(d, ".git") if git_marker else d)
        return d

    def answer(self, worker, path):
        code, err, record = self.stop_pod(worker, {"CW_WORKDIR": path})
        return code, err, record

    def assert_pod_kept(self, worker, path):
        code, err, record = self.answer(worker, path)
        self.assertEqual(code, cw.EXIT_HOOK, err)
        self.assertIsNotNone(self.pod(worker))
        self.assertNotIn(SENTENCE, json.dumps(record["end_hook"]) if record else "")

    def assert_reports_none(self, worker, path):
        code, err, record = self.answer(worker, path)
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.pod(worker))
        self.assertEqual(record["end_hook"]["note"], SENTENCE)

    # ---- S6 ----
    def test_a_repository_directory_of_mode_000_keeps_the_pod(self):
        self.need_user()
        d = self.repo_dir()
        self.set_mode(d, 0)
        self.assert_pod_kept("coder4", d)

    def test_a_repository_directory_of_mode_600_keeps_the_pod(self):
        self.need_user()
        d = self.repo_dir()
        self.set_mode(d, 0o600)
        self.assert_pod_kept("coder4", d)

    def test_a_repository_under_a_parent_that_cannot_be_entered_keeps_the_pod(self):
        self.need_user()
        d = self.repo_dir()
        self.set_mode(self.space, 0)
        self.assert_pod_kept("coder4", d)

    def test_a_dangling_link_at_the_path_keeps_the_pod(self):
        d = os.path.join(self.space, "repo")
        os.symlink(os.path.join(self.space, "nowhere"), d)
        self.assert_pod_kept("coder4", d)

    def test_a_git_file_pointing_nowhere_is_a_checkout_and_a_failing_push_keeps_the_pod(self):
        d = os.path.join(self.space, "repo")
        os.makedirs(d)
        with open(os.path.join(d, ".git"), "w") as f:
            f.write("gitdir: /nonexistent/place\n")
        self.assert_pod_kept("coder4", d)

    def test_an_absent_directory_and_a_readable_empty_one_report_no_repository(self):
        self.assert_reports_none("coder4", os.path.join(self.space, "repo"))
        d = self.repo_dir(git_marker=False)
        self.assert_reports_none("coder5", d)

    # ---- S7: an absent repository directory, a workspace with ANY entry ----
    def test_a_workspace_with_any_entry_keeps_the_pod(self):
        import subprocess
        makers = {
            "depth1": lambda s: os.makedirs(os.path.join(s, "other", ".git")),
            "depth4": lambda s: os.makedirs(os.path.join(s, "a", "b", "c", "d", ".git")),
            "file": lambda s: open(os.path.join(s, "notes.txt"), "w").close(),
            "dir": lambda s: os.makedirs(os.path.join(s, "plain")),
            "bare": lambda s: subprocess.run(["git", "init", "-q", "--bare", os.path.join(s, "b.git")], check=True),
            "many": lambda s: [open(os.path.join(s, "f%d" % i), "w").close() for i in range(3000)],
        }
        for i, (name, make) in enumerate(makers.items()):
            space = os.path.join(self.tmp.name, "ws-" + name)
            os.makedirs(space)
            make(space)
            with self.subTest(name):
                self.assert_pod_kept("keep%d" % i, os.path.join(space, "repo"))

    def test_an_empty_workspace_and_an_absent_one_report_no_repository(self):
        self.assert_reports_none("coder4", os.path.join(self.space, "repo"))
        self.assert_reports_none("coder5", os.path.join(self.tmp.name, "gone", "repo"))

    # ---- S8: a readable repository directory without a marker but with an entry ----
    def test_a_readable_directory_with_an_entry_and_no_git_keeps_the_pod(self):
        import subprocess
        makers = {
            "file": lambda d: open(os.path.join(d, "notes.txt"), "w").close(),
            "bare": lambda d: subprocess.run(["git", "init", "-q", "--bare", d], check=True),
            "tree": lambda d: (os.makedirs(os.path.join(d, "src", "pkg")), open(os.path.join(d, "src", "pkg", "m.py"), "w").close()),
            "hidden": lambda d: open(os.path.join(d, ".hidden"), "w").close(),
        }
        for i, (name, make) in enumerate(makers.items()):
            d = os.path.join(self.tmp.name, "rd-" + name, "repo")
            os.makedirs(d)
            make(d)
            with self.subTest(name):
                self.assert_pod_kept("rd%d" % i, d)

    def test_a_directory_with_a_git_marker_is_a_checkout(self):
        import end_hook
        d = self.repo_dir()
        self.assertIs(end_hook.no_checkout(d), False)

    # ---- S9: the pre-stop entry point ----
    def test_prestop_says_cannot_read_for_a_directory_that_cannot_be_searched(self):
        self.need_user()
        out = self.prestop_output()
        self.assertIn("cannot read", out)
        self.assertNotIn("no git repository here", out)

    def prestop_output(self, patch=None):
        import contextlib
        import io
        import end_hook
        d = self.repo_dir()
        self.set_mode(d, 0o600)
        env = {"CW_WORKDIR": d, "CW_WORKER": "coder4"}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                end_hook.prestop_main()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return buf.getvalue()

    # ---- U3: the decision has no exists/isdir/listdir and no bound ----
    def test_the_decision_functions_hold_no_exists_check_and_no_bound(self):
        import inspect
        import end_hook
        src = inspect.getsource(end_hook.no_checkout) + inspect.getsource(end_hook.probe_path) + inspect.getsource(end_hook.has_marker) \
            + inspect.getsource(end_hook.first_entry)
        for word in ("os.path.exists", "os.path.isdir", "os.path.isfile", "listdir", "DEPTH", "MAX_ENTRIES"):
            self.assertNotIn(word, src)
        self.assertFalse(hasattr(end_hook, "scan_for_checkout"))

    # ---- mutants (in memory, applied counter) ----
    def test_mutant_deciding_with_exists_reports_the_unreadable_states(self):
        import end_hook
        self.need_user()
        applied = []

        def old(workdir):
            applied.append(1)
            return not os.path.exists(os.path.join(workdir, ".git"))
        d = self.repo_dir()
        self.set_mode(d, 0)
        orig = end_hook.no_checkout
        end_hook.no_checkout = old
        self.addCleanup(setattr, end_hook, "no_checkout", orig)
        self.assertIs(end_hook.run(d, "coder4")["no_repository"], True)   # the mutant loses the work: the real checks above fail for it
        self.assertTrue(applied)
        end_hook.no_checkout = orig
        with self.assertRaises(OSError):
            end_hook.run(d, "coder4")

    def hook_with(self, fn):
        import end_hook
        orig = end_hook.no_checkout
        end_hook.no_checkout = fn
        self.addCleanup(setattr, end_hook, "no_checkout", orig)
        return end_hook, orig

    def mutant_row(self, fn, setup, name):
        """The mutant answers no_repository where the real hook raises; mutation asserted applied."""
        applied = []

        def wrapped(w):
            applied.append(1)
            return fn(w)
        end_hook, orig = self.hook_with(wrapped)
        self.assertIs(end_hook.run(setup, name)["no_repository"], True)
        self.assertTrue(applied)
        end_hook.no_checkout = orig
        with self.assertRaises((RuntimeError, OSError)):
            end_hook.run(setup, name)

    def test_mutant_readable_directory_without_marker_is_no_repository(self):
        d = os.path.join(self.tmp.name, "m8", "repo")
        os.makedirs(d)
        open(os.path.join(d, "f"), "w").close()
        self.mutant_row(lambda w: not os.path.isdir(os.path.join(w, ".git")), d, "coder4")

    def test_mutant_depth_3_scan_misses_depth_4(self):
        os.makedirs(os.path.join(self.space, "a", "b", "c", "d", ".git"))

        def scan(w):
            for root, dirs, _ in os.walk(w):
                if root[len(w):].count(os.sep) <= 3 and ".git" in dirs:
                    raise RuntimeError("found")
            return True
        self.mutant_row(scan, os.path.join(self.space, "repo"), "coder4")

    def test_mutant_entry_bound_of_2000_on_the_deletion_path(self):
        for i in range(3000):
            open(os.path.join(self.space, "f%d" % i), "w").close()

        def bounded(w):
            n = len(os.listdir(os.path.dirname(w)))
            if n > 2000:
                return True   # the mutant: too many entries to look at, so answer as if none
            raise RuntimeError("entries")
        self.mutant_row(bounded, os.path.join(self.space, "repo"), "coder4")

    def test_mutant_prestop_with_exists_prints_no_git_repository(self):
        self.need_user()
        import end_hook
        applied = []
        real = os.lstat

        def fake(path, *a, **k):
            if str(path).endswith(".git"):
                applied.append(1)
                raise FileNotFoundError(path)   # what os.path.exists makes of a permission error
            return real(path, *a, **k)
        os.lstat = fake
        try:
            out = self.prestop_output()
        finally:
            os.lstat = real
        self.assertTrue(applied)
        self.assertIn("no git repository here", out)

    def test_the_documentation_states_the_condition_and_the_limits(self):
        with open(DOC, encoding="utf-8") as f:
            text = " ".join(f.read().split())
        for phrase in ("nothing can be lost, on this condition", "absent or empty AND the workspace", "ANY entry in the workspace",
                       "keeps the pod", "the reaper", "recover route", "only delete its own pod", "pushes from the workspace only"):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
