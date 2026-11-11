"""S5 of #1027: the pod side needs no change. The credential helper and the gh wrapper the REAL entrypoint generates read the git-token file at every call,
so a Secret the kubelet refreshes (an atomic swap of the directory link, a rename, or an in-place write) reaches the next call, and an emptied file fails it.
The entrypoint harness is the worker suite's own (cloud/worker/tests/test_entrypoint.py)."""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "worker", "tests"))
import test_entrypoint as te  # noqa: E402



class ReadAtCall(unittest.TestCase):
    setUp = te.Entrypoint.setUp
    put = te.Entrypoint.put
    env = te.Entrypoint.env
    start = te.Entrypoint.start

    def ask(self, helper):
        return subprocess.run([helper, "get"], input="protocol=https\nhost=github.com\n\n", capture_output=True, text=True,
                              env={"PATH": self.bin + ":/usr/bin:/bin"})

    def gh_length(self):
        r = subprocess.run([os.path.join(self.tmpfs, "bin", "gh")], capture_output=True, text=True, env={"PATH": self.bin + ":/usr/bin:/bin"})
        return r

    def check(self, helper, token, cached=False):
        got = self.ask(helper)
        self.assertEqual(got.stdout, "username=x-access-token\npassword=%s\n" % token)
        self.assertIn("gh token length %d" % len(token), self.gh_length().stdout)

    def test_a_replaced_file_is_read_by_the_next_call_by_rename_by_symlink_swap_and_in_place(self):
        self.assertEqual(self.start().returncode, 0)
        helper = os.path.join(self.tmpfs, "bin", "git-credential-cw")
        path = os.path.join(self.run_dir, "git-token")
        self.check(helper, te.GIT)
        new1, new2, new3 = "n1" + "a" * 11, "n2" + "b" * 17, "n3" + "c" * 23   # lengths differ, so the gh stub tells them apart
        scratch = os.path.join(self.run_dir, "git-token.new")   # a rename: the file is a new inode
        with open(scratch, "w") as f:
            f.write(new1)
        os.replace(scratch, path)
        self.check(helper, new1)
        # the kubelet's way: git-token is a link into a data directory, and the directory link is swapped atomically
        os.remove(path)
        for d, tokenvalue in (("..v1", new2), ("..v2", new3)):
            os.mkdir(os.path.join(self.run_dir, d))
            with open(os.path.join(self.run_dir, d, "git-token"), "w") as f:
                f.write(tokenvalue)
        os.symlink("..data/git-token", path)
        os.symlink("..v1", os.path.join(self.run_dir, "..data"))
        self.check(helper, new2)
        os.symlink("..v2", os.path.join(self.run_dir, "..data.tmp"))
        os.replace(os.path.join(self.run_dir, "..data.tmp"), os.path.join(self.run_dir, "..data"))
        self.check(helper, new3)
        os.remove(path)   # in place, last: an ordinary file rewritten under the same name
        with open(path, "w") as f:
            f.write("in" + "d" * 9)
        self.check(helper, "in" + "d" * 9)
        with open(path, "w") as f:
            f.write("")
        for r in (self.ask(helper), self.gh_length()):
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(len(r.stderr.strip().splitlines()), 1)

    def test_a_helper_that_caches_the_token_is_caught_by_the_check(self):
        self.assertEqual(self.start().returncode, 0)
        helper = os.path.join(self.tmpfs, "bin", "git-credential-cw")
        with open(helper, "w") as f:   # the mutant: the token as it was at start, never read again
            f.write("#!/bin/sh\nprintf 'username=x-access-token\\npassword=%s\\n' '" + te.GIT + "'\n")
        path = os.path.join(self.run_dir, "git-token")
        os.chmod(path, 0o600)
        with open(path, "w") as f:
            f.write("rotated-token")
        with self.assertRaises(AssertionError):
            self.check(helper, "rotated-token")


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if result.wasSuccessful() else 1)
