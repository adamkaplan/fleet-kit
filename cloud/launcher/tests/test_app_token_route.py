"""Offline tests for the route of the App token mint (ask #1043): the token goes only to a route the caller names (--fd N or --out PATH),
never to standard output or standard error. The cause: the mint used to print the token on plain stdout, the channel that terminals,
transcripts and logs capture by default, so a slip in a redirect or a debugging run showed a one-hour token. Everything runs against the
loopback fake GitHub and a throwaway key of test_app_token.py; a canary token in the fake's answer must never be seen on a screen."""
import contextlib
import importlib.util
import os
import pty
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import test_app_token as ta  # noqa: E402
import cloud_worker as cw  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
MINT, TOKEN, REAL = ta.MINT, ta.TOKEN, ta.REAL


class Route(unittest.TestCase):
    setUpClass = classmethod(ta.Mint.setUpClass.__func__)
    tearDownClass = classmethod(ta.Mint.tearDownClass.__func__)
    setUp = ta.Mint.setUp

    def env(self):
        return dict(os.environ, CW_APP_ID_CMD="echo 12345", CW_APP_KEY_CMD="cat '%s'" % self.keyfile, CW_GITHUB_API_BASE=self.base, CW_OPENSSL=self.wrap)

    def run_it(self, args, path=MINT, fds=()):
        """(completed process with its real stdout and stderr, nothing else): the mint run with args and the descriptors in fds inherited."""
        return subprocess.run([sys.executable, path, REAL] + list(args), env=self.env(), capture_output=True, text=True, timeout=60, pass_fds=tuple(fds))

    def run_fd(self, path=MINT):
        r, w = os.pipe()
        try:
            p = self.run_it(["--fd", str(w)], path, (w,))
        finally:
            os.close(w)
        with os.fdopen(r, "rb") as f:
            return p, f.read()

    def private_dir(self):
        d = tempfile.mkdtemp(dir=self.tmp.name)
        os.chmod(d, 0o700)
        return d

    def mutant(self, old, new, *more):
        with open(MINT) as f:
            src = f.read()
        for o, n in ((old, new),) + more:
            self.assertIn(o, src, o)
            src = src.replace(o, n, 1)
        d = tempfile.mkdtemp(dir=self.tmp.name)
        with open(os.path.join(os.path.dirname(MINT), "token_mint.py")) as f, open(os.path.join(d, "token_mint.py"), "w") as g:
            g.write(f.read())
        path = os.path.join(d, "app_token.py")
        with open(path, "w") as f:
            f.write(src)
        return path

    # -- the checks, run on the real mint and on each mutant --
    def check_no_route(self, path=MINT):
        Stub = ta.Stub
        Stub.deletes = []
        p = self.run_it([], path)
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        self.assertNotIn(TOKEN, p.stderr)
        self.assertEqual(p.stderr.strip().count("\n"), 0)
        self.assertIn(str(len(TOKEN)), p.stderr)
        self.assertEqual(len(Stub.deletes), 1)   # nobody received it: revoked

    def check_fd(self, path=MINT):
        p, got = self.run_fd(path)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(got, TOKEN.encode())   # exactly the token, no newline
        self.assertEqual(p.stdout, "")
        self.assertNotIn(TOKEN, p.stderr)

    def check_out(self, path=MINT):
        d = self.private_dir()
        target = os.path.join(d, "tok")
        p = self.run_it(["--out", target], path)
        self.assertEqual((p.returncode, p.stdout), (0, ""), p.stderr)
        self.assertNotIn(TOKEN, p.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        with open(target) as f:
            self.assertEqual(f.read(), TOKEN)
        q = self.run_it(["--out", target], path)   # a second run on the same path
        self.assertEqual((q.returncode, q.stdout), (1, ""))
        self.assertNotIn(TOKEN, q.stderr)
        with open(target) as f:
            self.assertEqual(f.read(), TOKEN)

    # -- DW-2 --
    def test_s2_no_route_says_only_the_byte_count_and_exits_0(self):
        self.check_no_route()

    def test_s3_a_rejected_mint_without_a_route_never_shows_the_token(self):
        for bad in ({"contents": "write", "pull_requests": "write", "issues": "read", "metadata": "read", "actions": "read"},
                    {"contents": "write"}):
            ta.Stub.answer["permissions"] = bad
            ta.Stub.deletes = []
            p = self.run_it([])
            self.assertEqual((p.returncode, p.stdout), (1, ""))
            self.assertNotIn(TOKEN, p.stdout + p.stderr)
            self.assertEqual(len(ta.Stub.deletes), 1)   # revoke-on-refusal unchanged
        ta.Stub.answer["repositories"] = [{"name": "other", "full_name": "acme-org/other", "owner": {"id": 9}}]
        p = self.run_it([])
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertNotIn(TOKEN, p.stderr)

    def test_s4_a_descriptor_route_gets_exactly_the_token(self):
        self.check_fd()

    def test_s5_a_bad_descriptor_route_is_stopped_before_any_mint(self):
        r, w = os.pipe()
        master, slave = pty.openpty()
        cases = {"0": (), "1": (), "2": (), "99": (), "read-only": (r,), "terminal": (slave,)}
        args = {"0": "0", "1": "1", "2": "2", "99": "99", "read-only": str(r), "terminal": str(slave)}
        try:
            for name, fds in cases.items():
                ta.Stub.seen, ta.Stub.gets = [], []
                p = self.run_it(["--fd", args[name]], fds=fds)
                self.assertEqual(p.returncode, 2, (name, p.stderr))
                self.assertEqual(p.stdout, "", name)
                self.assertEqual((ta.Stub.seen, ta.Stub.gets), ([], []), name)
            both = self.run_it(["--fd", str(w), "--out", os.path.join(self.private_dir(), "t")], fds=(w,))
            self.assertEqual(both.returncode, 2)
            self.assertEqual(ta.Stub.seen, [])
        finally:
            for fd in (r, w, master, slave):
                os.close(fd)

    def test_s6_a_path_route_is_created_owner_only_and_never_overwritten(self):
        self.check_out()

    def test_s7_a_path_route_in_a_bad_directory_or_a_rejected_mint_creates_nothing(self):
        wide = tempfile.mkdtemp(dir=self.tmp.name)
        os.chmod(wide, 0o755)
        real = self.private_dir()
        link = os.path.join(self.tmp.name, "link")
        os.symlink(real, link)
        for name, d in (("group or world accessible", wide), ("a link", link), ("missing", os.path.join(real, "no-such"))):
            p = self.run_it(["--out", os.path.join(d, "tok")])
            self.assertNotEqual(p.returncode, 0, name)
            self.assertNotIn(TOKEN, p.stdout + p.stderr, name)
            self.assertEqual(ta.Stub.seen, [], name)
        self.assertEqual(os.listdir(wide) + os.listdir(real), [])
        ta.Stub.answer["permissions"] = {"contents": "write"}
        p = self.run_it(["--out", os.path.join(real, "tok")])
        self.assertEqual(p.returncode, 1)
        self.assertEqual(os.listdir(real), [])
        self.assertNotIn(TOKEN, p.stdout + p.stderr)

    def test_s8_a_delivery_that_fails_revokes_the_token_and_exits_non_zero(self):
        r, w = os.pipe()
        os.close(r)   # nobody reads: a closed pipe
        try:
            ta.Stub.deletes = []
            p = self.run_it(["--fd", str(w)], fds=(w,))
        finally:
            os.close(w)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(len(ta.Stub.deletes), 1)
        self.assertNotIn(TOKEN, p.stdout + p.stderr)

    # -- DW-4: the route does not change a check --
    def test_s15_a_rejected_mint_delivers_nothing_to_a_route_and_revokes_as_before(self):
        ta.Stub.answer["permissions"] = {"contents": "write"}
        ta.Stub.deletes = []
        p, got = self.run_fd()
        self.assertEqual((p.returncode, got, p.stdout), (1, b"", ""))
        self.assertEqual(len(ta.Stub.deletes), 1)

    # -- DW-5: each mutant is caught --
    def caught(self, check, old, new, *more):
        path = self.mutant(old, new, *more)
        with self.assertRaises(AssertionError):
            check(path)

    def test_s13_mutants_that_show_the_token_or_weaken_the_path_route_fail(self):
        self.check_no_route()
        self.check_fd()
        self.check_out()
        self.caught(self.check_no_route, "        revoke(token)  # nobody received it", "        print(token); revoke(token)")
        self.caught(self.check_fd, "        write_all(value, data)\n        return", "        print(token)\n        write_all(value, data)\n        return")
        self.caught(self.check_out, "    os.close(fd)\n\n\ndef main", "    print(token)\n    os.close(fd)\n\n\ndef main")
        self.caught(self.check_out, "os.O_EXCL | os.O_NOFOLLOW, 0o600", "os.O_NOFOLLOW, 0o600",   # no exclusivity (and no early check, which is a second line of defence)
                    ('    if os.path.lexists(path):\n        die("refusing: --out path exists; the mint never overwrites", 1)\n', ""))
        self.caught(self.check_out, "os.O_EXCL | os.O_NOFOLLOW, 0o600", "os.O_EXCL | os.O_NOFOLLOW, 0o644")

    # -- DW-3: the launcher's two default calls --
    def launcher_env(self):
        e = self.env()
        e.pop("CW_GIT_TOKEN_CMD", None)
        return e

    def spy_run(self):
        calls = []
        real = subprocess.run

        def spy(argv, *a, **k):
            calls.append(list(argv))
            return real(argv, *a, **k)
        return calls, mock.patch.object(cw.subprocess, "run", spy)

    def test_s10_the_launcher_helper_gets_the_default_token_through_a_descriptor(self):
        heard = []
        calls, patch = self.spy_run()
        with patch:
            token = cw.run_git_token_cmd(cw.git_token_cmd(REAL, self.launcher_env()), REAL, self.launcher_env(), register=heard.append)
        self.assertEqual(token, TOKEN)
        self.assertEqual(calls[0][-2], "--fd")
        self.assertIn(TOKEN, heard)   # registered with the scrubber

    def test_s10_the_cluster_launch_path_gets_the_default_token_through_a_descriptor(self):
        spec = importlib.util.spec_from_file_location("bin_cloud_worker_route", os.path.join(ROOT, "bin", "cloud_worker.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["bin_cloud_worker_route"] = mod
        spec.loader.exec_module(mod)

        class Cluster:
            def clean(self, text, n):
                return text[:n]
        calls, patch = self.spy_run()
        with patch, mock.patch.dict(os.environ, self.launcher_env(), clear=True):
            self.assertEqual(mod.git_token(REAL, Cluster()), TOKEN)
        self.assertEqual(calls[0][-2], "--fd")

    def test_s11_an_override_command_still_prints_on_stdout_and_is_registered(self):
        heard = []
        calls, patch = self.spy_run()
        env = dict(self.launcher_env(), CW_GIT_TOKEN_CMD="echo %s" % TOKEN)
        with patch:
            token = cw.run_git_token_cmd(cw.git_token_cmd(REAL, env), REAL, env, register=heard.append)
        self.assertEqual(token, TOKEN)
        self.assertNotIn("--fd", calls[0])
        self.assertIn(TOKEN, heard)

    def test_s12_a_failing_command_registers_the_token_before_judging_and_never_echoes_it(self):
        heard = []
        env = dict(self.launcher_env(), CW_GIT_TOKEN_CMD="sh -c 'echo %s; exit 1'" % TOKEN)
        with self.assertRaises(cw.CloudError) as ctx:
            cw.run_git_token_cmd(cw.git_token_cmd(REAL, env), REAL, env, register=heard.append)
        self.assertIn(TOKEN, heard)
        self.assertNotIn(TOKEN, str(ctx.exception))

    # -- DW-3, DW-6: every file that names the mint is in one table; the documentation shows only safe runs --
    CALLERS = {   # file: how it reaches the mint
        "bin/cloud_worker.py": "fd (the launcher's helper)",
        "bin/test-cloud-launcher.chain": "text (the test list)",
        "bin/test-cloud-launcher.parallel": "text (the test list)",
        "bin/test-switchboard": "text (a hygiene list)",
        "cloud/README.md": "text",
        "cloud/launcher/README.md": "text",
        "cloud/launcher/app_token.py": "the mint itself",
        "cloud/launcher/cloud_worker.py": "fd (the one helper; an override prints on stdout)",
        "cloud/launcher/preflight.py": "in process (no output)",
        "cloud/launcher/run_task.py": "fd (through the launcher's helper)",
        "cloud/launcher/tests/test_app_token.py": "fd",
        "cloud/launcher/tests/test_app_token_route.py": "fd, path and none",
        "cloud/launcher/tests/test_chain_list.py": "text (the test list)",
        "cloud/launcher/tests/test_chain_time.py": "text (the test list)",
        "cloud/launcher/tests/test_driver.py": "text (the default command is pinned)",
        "cloud/launcher/tests/test_git_token_renewal.py": "fd (through the launcher's helper)",
        "cloud/launcher/tests/test_issues_read.py": "in process and fd",
        "cloud/launcher/tests/test_preflight.py": "in process",
        "docs/agent-credentials.md": "text",
        "docs/cloud-operator-quickstart.md": "text",
        "docs/review-laws/impact-matrix.json": "text (a path list)",
    }

    def test_s9_every_file_that_names_the_mint_is_in_the_table_and_none_reads_stdout(self):
        listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
        found = set()
        for name in filter(None, listed):
            if os.path.islink(os.path.join(ROOT, name)):
                continue
            try:
                with open(os.path.join(ROOT, name), errors="replace") as f:
                    if "app_token" in f.read():
                        found.add(name)
            except OSError:
                pass
        self.assertEqual((sorted(found - set(self.CALLERS)), sorted(set(self.CALLERS) - found)), ([], []))
        self.assertFalse([f for f, route in self.CALLERS.items() if "stdout" in route.replace("an override prints on stdout", "")])

    def test_s14_the_documents_say_how_to_run_the_mint_by_hand_safely(self):
        docs = ["cloud/launcher/README.md", "docs/cloud-operator-quickstart.md", "docs/agent-credentials.md", "cloud/README.md"]
        for name in docs:
            with open(os.path.join(ROOT, name)) as f:
                for n, line in enumerate(f, 1):
                    self.assertIsNone(re.search(r"(\$\(|`)[^`)]*python3?\s+\S*app_token\.py", line), (name, n))   # a command substitution
                    self.assertIsNone(re.search(r"python3?\s+\S*app_token\.py[^\n]*(\s>|\s\|\s)", line), (name, n))   # a redirect or a pipe
                    self.assertNotIn("prints a one-hour GitHub App installation token", line, (name, n))
        with open(os.path.join(ROOT, "cloud/launcher/README.md")) as f:
            readme = f.read()
        self.assertIn("Running the mint by hand", readme)
        self.assertIn("--out", readme)
        self.assertIn("rm ", readme[readme.index("Running the mint by hand"):])
        self.assertNotIn("prints the one-hour git token for the repository (default", readme)


if __name__ == "__main__":
    unittest.main()
