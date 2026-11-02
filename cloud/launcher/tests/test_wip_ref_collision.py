"""Ask #1864: a worker named like its branch cannot save work to `<worker>/wip` (git cannot hold both refs).

Scenarios S2 (git itself), S3 (the launch refusal), S4 (the document), S6 (the end hook's fallback ref) and S7 (the stop line and the lane's
note). Offline; temporary repositories are removed; a thread, if one is ever started here, is a daemon thread (daemon=True). Every mutant is a scratch
copy of the real text: the text is asserted to occur once, the mutation asserted applied, and the unmutated code is the control run FIRST."""
import contextlib
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
import test_cloud_worker as launcher_tests  # noqa: E402  (the launcher's fake cluster; its fixtures)

_spec = importlib.util.spec_from_loader("wip_bin_tests", importlib.machinery.SourceFileLoader("wip_bin_tests", os.path.join(ROOT, "bin", "test-cloud-worker")))
bin_tests = importlib.util.module_from_spec(_spec)
sys.modules["wip_bin_tests"] = bin_tests
_spec.loader.exec_module(bin_tests)

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"}


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, env=dict(os.environ, **GIT_ENV), check=check, capture_output=True, text=True)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def scratch(tmp, worker, with_branch, untracked=False):
    """A bare repository (with the branch `worker` when asked) and a clone whose HEAD is one commit on no remote branch (or has an untracked file)."""
    bare, work = os.path.join(tmp, "o.git"), os.path.join(tmp, "work")
    git(tmp, "init", "-q", "--bare", bare)
    git(tmp, "init", "-q", "-b", "base", work)
    git(work, "remote", "add", "origin", bare)
    with open(os.path.join(work, "a.txt"), "w") as f:
        f.write("a\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "first")
    git(work, "push", "-q", "origin", "base")
    if with_branch:
        git(work, "push", "-q", "origin", "base:refs/heads/" + worker)
    git(work, "fetch", "-q", "origin")
    with open(os.path.join(work, "b.txt"), "w") as f:
        f.write("b\n")
    if not untracked:
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "second")
    return bare, work


def heads(bare):
    return [h for h in git(bare, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads").stdout.split("\n") if h]


class S2Git(unittest.TestCase):
    def test_s2_git_refuses_the_ref_beside_the_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "a"))
            os.makedirs(os.path.join(tmp, "b"))
            bare, work = scratch(os.path.join(tmp, "a"), "w", True)
            p = git(work, "push", bare, "HEAD:refs/heads/w/wip", check=False)
            print("S2 refused:", p.returncode != 0, "|", (p.stderr.strip().splitlines() or [""])[0][:120])
            self.assertNotEqual(p.returncode, 0)
            bare2, work2 = scratch(os.path.join(tmp, "b"), "w", False)
            q = git(work2, "push", bare2, "HEAD:refs/heads/w/wip", check=False)
            names = [h.split()[0] for h in heads(bare2)]
            print("S2 accepted without the branch:", q.returncode == 0, "| branches:", names)
            self.assertEqual(q.returncode, 0)
            self.assertIn("w/wip", names)


class S3Launch(bin_tests.Base):
    def equal_launch(self):
        brief = self.home / "brief.txt"
        brief.write_text("Build the thing.\n")
        code, out, err = self.main("launch", "main2", "--cloud", "--agent", "coder", "--repo", "acme-org/widget-service", "--branch", "main2",
                                   "--role", "coder", "--reports-to", "orch", "--brief-file", str(brief))
        if "have the same name" not in err:   # past the check: accepted (a later stop of the fake cluster is not the refusal)
            raise AssertionError("a launch with the worker's name as its branch was accepted")
        return code, [l for l in err.splitlines() if l.strip()]

    def test_s3_refused_before_anything_is_created(self):
        code, lines = self.equal_launch()
        calls, record = self.stub_calls(), (self.state / "cloud" / "main2.json").exists()
        print("S3 equal: exit", code, "lines", len(lines), "calls", len(calls), "record", record, "|", lines[0][:160])
        self.assertEqual((code, len(lines), calls, record), (1, 1, [], False))   # exit 1: the status of the bad-branch refusal (LaunchError)
        self.assertIn("main2", lines[0])
        self.assertIn("main2/wip", lines[0])
        other = self.launch_with("--cloud", name="coder7")   # a different branch (main): accepted as before
        print("S3 different branch: exit", other[0])
        self.assertEqual(other[0], 0)

    def test_s3_the_copy_without_the_check_fails(self):
        self.equal_launch()   # the control run first: the real launch refuses
        path = os.path.join(ROOT, "bin", "cloud_worker.py")
        src = read("bin/cloud_worker.py")
        old = "    if args.name == branch:\n"
        self.assertEqual(src.count(old), 1)
        mutant = src.replace(old, "    if False:\n")
        self.assertNotEqual(mutant, src)
        mod = importlib.util.module_from_spec(importlib.util.spec_from_file_location("cloud_worker", path))
        mod.__file__ = path
        exec(compile(mutant, path, "exec"), mod.__dict__)
        with mock.patch.dict(bin_tests.sb._MODULES_DONE, {"cloud_worker": mod}), mock.patch.dict(sys.modules, {"cloud_worker": mod}):
            with self.assertRaises(AssertionError) as caught:
                self.equal_launch()
        print("S3 mutant:", caught.exception)
        self.assertEqual(str(caught.exception), "a launch with the worker's name as its branch was accepted")


PHRASES = ("A worker never works on a branch named like itself", "The launch refuses a branch equal to the worker's name",
           "The end hook pushes to `<worker>-wip` when `<worker>/wip` is blocked")


def document_check(text):
    found = [p for p in PHRASES if p in text]
    if len(found) != 3:
        raise AssertionError("the document does not name the branch rule")
    return found


class S4Document(unittest.TestCase):
    def test_s4_the_document_names_the_rule(self):
        text = read("cloud/README.md")
        print("S4 control: sentences found", len(document_check(text)), "of 3")
        line = next(l for l in text.split("\n") if l.startswith("- **A branch named like the worker (#1864).**"))
        self.assertEqual(text.count(line), 1)
        mutant = text.replace(line, "")
        self.assertNotEqual(mutant, text)
        with self.assertRaises(AssertionError) as caught:
            document_check(mutant)
        print("S4 mutant:", caught.exception)
        self.assertEqual(str(caught.exception), "the document does not name the branch rule")


def load_hook(path):
    sp = importlib.util.spec_from_file_location("end_hook_wip", path)
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


class S6Hook(unittest.TestCase):
    def blocked_check(self, hook):
        with tempfile.TemporaryDirectory() as tmp:
            bare, work = scratch(tmp, "w", True, untracked=True)
            before = [h for h in heads(bare) if h.startswith("w ")]
            try:
                res = hook.run(work, "w")
            except RuntimeError:
                raise AssertionError("the blocked ref was tried") from None
            return res, before == [h for h in heads(bare) if h.startswith("w ")], [h.split()[0] for h in heads(bare)]

    def test_s6_blocked_and_normal(self):
        hook = load_hook(os.path.join(LAUNCHER, "end_hook.py"))
        res, unchanged, names = self.blocked_check(hook)
        print("S6 blocked:", res["wip_branch"], "blocked_by", res.get("wip_blocked_by"), "branch w unchanged", unchanged, names)
        self.assertEqual((res["pushed"], res["wip_branch"], res["wip_blocked_by"]), (True, "w-wip", "w"))
        self.assertTrue(unchanged)
        self.assertIn("w-wip", names)
        with tempfile.TemporaryDirectory() as tmp:
            bare, work = scratch(tmp, "w", False, untracked=True)
            res = hook.run(work, "w")
            print("S6 normal:", res)
            self.assertEqual((res["pushed"], res["wip_branch"]), (True, "w/wip"))
            self.assertNotIn("wip_blocked_by", res)

    def test_s6_the_copy_without_the_detection_fails(self):
        self.blocked_check(load_hook(os.path.join(LAUNCHER, "end_hook.py")))   # the control first
        src = read("cloud/launcher/end_hook.py")
        old = "    branch, blocked_by = wip_branch_for(workdir, worker, ctx)\n"
        self.assertEqual(src.count(old), 1)
        mutant = src.replace(old, '    branch, blocked_by = "%s/wip" % worker, None\n')
        self.assertNotEqual(mutant, src)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "end_hook.py")
            with open(path, "w") as f:
                f.write(mutant)
            with self.assertRaises(AssertionError) as caught:
                self.blocked_check(load_hook(path))
        print("S6 mutant:", caught.exception)
        self.assertEqual(str(caught.exception), "the blocked ref was tried")


class S7Stop(launcher_tests.Base):
    NEW = {"pushed": True, "wip_branch": "coder7-wip", "wip_blocked_by": "coder7"}

    def stop_with(self, answer, cwmod=None):
        self.patch_state(hook_result=answer)
        self.create("coder7")
        with contextlib.ExitStack() as stack:
            if cwmod is not None:
                stack.enter_context(mock.patch.object(launcher_tests, "cw", cwmod))
            code, out, err = self.run_cli("stop", "coder7", "--reason", "operator")
        self.assertEqual(code, 0, err)
        return err

    def stop_line_check(self, err):
        if "coder7-wip (coder7/wip is blocked by the branch coder7)" not in err:
            raise AssertionError("the stop line does not name the fallback ref")

    def test_s7_the_stop_line_names_both_refs(self):
        err = self.stop_with(self.NEW)
        self.stop_line_check(err)
        print("S7 stop line:", [l for l in err.splitlines() if "blocked" in l])

    def test_s7_an_old_image_answer_gives_no_blocked_line(self):
        err = self.stop_with({"pushed": True, "wip_branch": "coder7/wip"})
        print("S7 old image: stderr lines", len(err.splitlines()), "blocked mentioned", "blocked" in err)
        self.assertNotIn("blocked", err)

    def test_s7_the_copy_that_drops_the_field_fails(self):
        self.stop_line_check(self.stop_with(self.NEW))   # the control first
        path = os.path.join(LAUNCHER, "cloud_worker.py")
        src = read("cloud/launcher/cloud_worker.py")
        old = 'isinstance(res.get("wip_blocked_by"), str) and view["wip_branch"] else ""'
        self.assertEqual(src.count(old), 1)
        mutant = src.replace(old, 'False else ""')
        self.assertNotEqual(mutant, src)
        mod = importlib.util.module_from_spec(importlib.util.spec_from_file_location("cloud_worker_mut", path))
        mod.__file__ = path
        exec(compile(mutant, path, "exec"), mod.__dict__)
        self.tmp.cleanup()
        self.setUp()
        with self.assertRaises(AssertionError) as caught:
            self.stop_line_check(self.stop_with(self.NEW, mod))
        print("S7 mutant:", caught.exception)
        self.assertEqual(str(caught.exception), "the stop line does not name the fallback ref")


class S7Note(bin_tests.Base):
    def retire_text(self, blocked):
        self.assertEqual(self.launch("coder7")[0], 0)
        text = self.stub.read_text()
        old = '"wip_branch": worker + "/wip"}'
        self.assertEqual(text.count(old), 1)
        if blocked:
            self.stub.write_text(text.replace(old, '"wip_branch": worker + "-wip", "wip_blocked_by": worker}'))
        code, out, err = self.main("retire", "coder7")
        self.assertEqual(code, 0, err)
        return out

    def test_s7_the_note_names_both_refs(self):
        out = self.retire_text(True)
        print("S7 note:", out.strip()[:200])
        self.assertIn("coder7-wip (coder7/wip is blocked by the branch coder7)", out)

    def test_s7_the_note_of_an_old_answer_is_today(self):
        out = self.retire_text(False)
        print("S7 note old:", out.strip()[:200])
        self.assertIn("work was pushed to branch coder7/wip", out)
        self.assertNotIn("blocked", out)


if __name__ == "__main__":
    unittest.main()
