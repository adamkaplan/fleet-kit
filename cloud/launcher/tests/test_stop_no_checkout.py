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
        with open(os.path.join(bare_dir, "notes.txt"), "w") as f:
            f.write("not a checkout\n")
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
        code, err, record = self.stop_pod("coder4", {"CW_WORKDIR": os.path.join(self.tmp.name, "nowhere")})
        self.assertEqual(code, 0, err)
        self.assertEqual(record["end_hook"]["note"], SENTENCE)
        self.assertIs(record["end_hook"]["pushed"], False)
        self.assertEqual(record["end_hook"]["error"], "")
        self.assertTrue(self.stop_audit("coder4")["ok"])
        self.assertIn(SENTENCE, err)

    def test_the_hook_tells_the_three_cases_apart(self):
        import end_hook
        missing = end_hook.run(os.path.join(self.tmp.name, "none"), "coder7")
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


if __name__ == "__main__":
    unittest.main()
