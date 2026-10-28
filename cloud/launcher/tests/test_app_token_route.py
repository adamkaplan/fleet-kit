"""Offline tests for the route of the App token mint (ask #1043): the token goes only to a route the caller names (--fd N or --out PATH),
never to standard output or standard error. The cause: the mint used to print the token on plain stdout, the channel that terminals,
transcripts and logs capture by default, so a slip in a redirect or a debugging run showed a one-hour token. Everything runs against the
loopback fake GitHub and a throwaway key of test_app_token.py; a canary token in the fake's answer must never be seen on a screen."""
import contextlib
import importlib.util
import os
import pty
import re
import signal
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

    # -- S16: a descriptor that is another name of standard output or standard error is refused before minting --
    def run_dup(self, which, path=MINT):
        """(process, stub requests seen): --fd N where N is a second descriptor of the very pipe (which = 'out') or file (which = 'err') the run's std stream is."""
        ta.Stub.seen, ta.Stub.gets = [], []
        if which == "out":
            r, w = os.pipe()
            try:
                p = subprocess.run([sys.executable, path, REAL, "--fd", str(w)], env=self.env(), stdout=w, stderr=subprocess.PIPE, text=True, timeout=60, pass_fds=(w,))
            finally:
                os.close(w)
            with os.fdopen(r, "rb") as f:
                seen = f.read()
        else:
            with tempfile.TemporaryFile() as f:
                p = subprocess.run([sys.executable, path, REAL, "--fd", str(f.fileno())], env=self.env(), stdout=subprocess.PIPE, stderr=f, text=True, timeout=60, pass_fds=(f.fileno(),))
                f.seek(0)
                seen = f.read()
        return p, seen, list(ta.Stub.seen) + list(ta.Stub.gets)

    def check_dups(self, path=MINT):
        for which in ("out", "err"):
            p, seen, requests = self.run_dup(which, path)
            self.assertEqual(p.returncode, 2, (which, p.stderr))
            self.assertEqual(requests, [], which)
            self.assertNotIn(TOKEN.encode(), seen, which)

    def test_s16_a_second_descriptor_of_standard_output_or_error_is_refused_before_minting(self):
        self.check_dups()
        p, got = self.run_fd()   # a separate pipe is still accepted
        self.assertEqual((p.returncode, got), (0, TOKEN.encode()), p.stderr)

    def test_s16_a_mutant_that_checks_only_0_1_2_fails(self):
        path = self.mutant("    if same_stream_as_output(fd):", "    if False:")
        with self.assertRaises(AssertionError):
            self.check_dups(path)

    def test_s7_a_link_in_any_ancestor_of_the_path_route_is_refused_before_minting(self):
        real = self.private_dir()
        os.mkdir(os.path.join(real, "inner"))
        os.chmod(os.path.join(real, "inner"), 0o700)
        link = os.path.join(self.tmp.name, "alink")
        os.symlink(real, link)
        ta.Stub.seen, ta.Stub.gets = [], []
        p = self.run_it(["--out", os.path.join(link, "inner", "tok")])
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertNotIn(TOKEN, p.stdout + p.stderr)
        self.assertEqual((ta.Stub.seen, ta.Stub.gets, os.listdir(os.path.join(real, "inner"))), ([], [], []))

    def test_s7_a_mutant_that_checks_only_the_immediate_parent_fails(self):
        path = self.mutant('    if link_in_parents(os.path.join(d, "x")):', "    if False:")
        real = self.private_dir()
        os.mkdir(os.path.join(real, "inner"))
        os.chmod(os.path.join(real, "inner"), 0o700)
        link = os.path.join(self.tmp.name, "blink")
        os.symlink(real, link)
        p = self.run_it(["--out", os.path.join(link, "inner", "tok")], path)
        self.assertEqual(p.returncode, 0)   # the mutant lets it through: the test above would fail on it

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

    def read_doc(self, name):
        with open(os.path.join(ROOT, name)) as f:
            return f.read()

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
        self.assertNotIn("Neither writes a file", readme)
        mint = readme[readme.index("**The cause (ask #1043):**"):]
        self.assertTrue(mint.startswith("**The cause (ask #1043):** the mint printed the one-hour repository token on standard output, where terminals, transcripts and logs capture it; two recorded cases"))
        self.assertIn("`3>&1`", readme)
        self.assertIn("symbolic link in any part of the directory path", readme)
        self.assertIn("the system's own root-owned links in a directory others cannot write, or a sticky one", readme)
        self.assertIn("a `..` component and every other link are refused", readme)
        real = "app-token: minted a token of %d bytes and did not deliver it: name a route with --fd N or --out PATH" % len(TOKEN)   # the text the mint prints (test_s2 runs it)
        self.assertIn(real, readme)
        self.assertIn("same pipe or file as standard output or standard error", self.read_doc("docs/cloud-operator-quickstart.md"))


class RouteLinks(unittest.TestCase):
    """Ask #1043, the findings of the independent proof: '..' is refused, only the system's own links pass, an interrupt revokes."""
    setUpClass = classmethod(ta.Mint.setUpClass.__func__)
    tearDownClass = classmethod(ta.Mint.tearDownClass.__func__)
    setUp = ta.Mint.setUp
    env, run_it, private_dir, mutant = Route.env, Route.run_it, Route.private_dir, Route.mutant

    def test_s17_a_dot_dot_component_is_refused_before_minting(self):
        real = self.private_dir()
        link = os.path.join(self.tmp.name, "dlink")
        os.symlink(real, link)
        for name, out in (("link/../x", os.path.join(link, "..", "x")), ("sub/../x", os.path.join(real, "sub", "..", "x")),
                          ("a/b/../../c", os.path.join(real, "a", "b", "..", "..", "c"))):
            ta.Stub.seen, ta.Stub.gets = [], []
            p = self.run_it(["--out", out])
            self.assertEqual(p.returncode, 2, name + p.stderr)
            self.assertEqual((ta.Stub.seen, ta.Stub.gets), ([], []), name)
        self.assertEqual(os.listdir(real), [])

    def test_s17_a_mutant_that_checks_dot_dot_lexically_fails(self):
        path = self.mutant('    if ".." in path.split("/"):', "    if False:")
        real = self.private_dir()
        os.mkdir(os.path.join(real, "sub"), 0o700)
        p = self.run_it(["--out", os.path.join(real, "sub", "..", "x")], path)
        self.assertEqual(p.returncode, 0)   # the mutant lets it through: the test above would fail on it

    def fake_fs(self, link_uid, parent_mode):
        """Patch lstat and readlink: /sys (a directory of parent_mode, uid 0) holds link -> real (a 0700 directory of this user)."""
        me = os.getuid()
        table = {"/": (stat.S_IFDIR | 0o755, 0), "/sys": (stat.S_IFDIR | parent_mode, 0), "/sys/link": (stat.S_IFLNK | 0o777, link_uid),
                 "/sys/real": (stat.S_IFDIR | 0o700, me)}

        def lstat(p):
            if p not in table:
                raise FileNotFoundError(p)
            mode, uid = table[p]
            return os.stat_result((mode, 1, 1, 1, uid, 0, 0, 0, 0, 0))
        return (mock.patch.object(os, "lstat", lstat), mock.patch.object(os, "readlink", lambda p: "real"),
                mock.patch.object(os, "getcwd", lambda: "/"), mock.patch.object(os.path, "realpath", lambda p: p.replace("/link", "/real")))

    def judge(self, link_uid, parent_mode):
        sys.path.insert(0, os.path.dirname(HERE))
        import app_token
        a, b, c, d = self.fake_fs(link_uid, parent_mode)
        with a, b, c, d:
            return app_token.link_in_parents("/sys/link/tok")

    def test_s17_only_a_root_made_link_in_a_directory_others_cannot_write_is_accepted(self):
        me = os.getuid()
        self.assertTrue(self.judge(me, 0o755), "made by the caller")
        self.assertTrue(self.judge(me + 1, 0o755), "made by another user")
        self.assertFalse(self.judge(0, 0o755), "root, 0755")
        self.assertFalse(self.judge(0, 0o1777), "root, sticky 1777")
        self.assertTrue(self.judge(0, 0o777), "root, 0777 not sticky")

    def test_s17_the_default_temporary_directory_is_accepted_for_real(self):
        self.assertFalse(link_in_parents_real(os.path.join(tempfile.gettempdir(), "x", "tok")))

    def test_s17_mutants_that_refuse_every_link_or_accept_every_link_fail(self):
        me = os.getuid()
        for old, new, case in (("    if link_st.st_uid != 0:\n        return False\n", "    return False\n", (0, 0o755, False)),   # refuses every link
                               ("    if link_st.st_uid != 0:\n        return False\n", "    return True\n", (me, 0o755, True))):   # accepts every link
            path = self.mutant(old, new)
            spec = importlib.util.spec_from_file_location("app_token_mutant", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            a, b, c, d = self.fake_fs(case[0], case[1])
            with a, b, c, d:
                self.assertNotEqual(mod.link_in_parents("/sys/link/tok"), case[2], case)

    def test_s17_the_other_shapes_of_a_bad_path_are_refused_before_minting(self):
        real = self.private_dir()
        open(os.path.join(real, "afile"), "w").close()
        cases = {"absent middle component": os.path.join(real, "absent", "x", "tok"), "a file in the middle": os.path.join(real, "afile", "tok"),
                 "a trailing slash": real + "/tok/", "a newline": os.path.join(real, "to\nk"), "a NUL": os.path.join(real, "to\0k"),
                 "a very long path": os.path.join(real, "d" * 5000, "tok"), "empty": ""}
        for name, out in cases.items():
            ta.Stub.seen, ta.Stub.gets = [], []
            try:
                p = self.run_it(["--out", out])
            except ValueError:   # a NUL cannot even be an argument: refused by the system before the program runs
                self.assertIn("NUL", name)
                continue
            self.assertEqual(p.returncode, 2, name + p.stderr)
            self.assertEqual((ta.Stub.seen, ta.Stub.gets), ([], []), name)
        self.assertEqual(os.listdir(real), ["afile"])
        # a double slash and a dot component are the same place, accepted and created
        p = self.run_it(["--out", real + "//./tok"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sorted(os.listdir(real)), ["afile", "tok"])

    def interrupted(self, mod, effect, patch_name="write_all"):
        real = self.private_dir()
        ta.Stub.deletes = []
        saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in saved.items()])
        with mock.patch.dict(os.environ, self.env()), mock.patch.object(mod, patch_name, side_effect=effect):
            try:
                mod.main([REAL, "--out", os.path.join(real, "tok")])
                code = 0
            except SystemExit as e:
                code = e.code
            except BaseException as e:   # an exception that the handler let out
                code = repr(e)
        return code, len(ta.Stub.deletes), os.listdir(real)

    def test_s17_any_failure_or_signal_during_delivery_revokes_removes_and_exits_non_zero(self):
        sys.path.insert(0, os.path.dirname(HERE))
        import app_token
        for name, effect in (("interrupt", KeyboardInterrupt), ("SIGTERM", lambda *a: os.kill(os.getpid(), signal.SIGTERM)),
                             ("SIGHUP", lambda *a: os.kill(os.getpid(), signal.SIGHUP)), ("broken pipe", BrokenPipeError),
                             ("disk full", OSError(28, "No space left on device")), ("any exception", RuntimeError)):
            code, deletes, left = self.interrupted(app_token, effect)
            self.assertIsInstance(code, int, name)
            self.assertNotEqual(code, 0, name)
            self.assertEqual((deletes, left), (1, []), name)
        with mock.patch.object(os, "fsync", side_effect=OSError(5, "io")):   # a failed flush
            code, deletes, left = self.interrupted(app_token, None)
        self.assertEqual((deletes, left), (1, []))

    def test_s17_an_interrupt_during_delivery_revokes_removes_the_file_and_exits_non_zero(self):
        sys.path.insert(0, os.path.dirname(HERE))
        import app_token
        real = self.private_dir()
        out = os.path.join(real, "tok")
        ta.Stub.deletes = []
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        with mock.patch.dict(os.environ, self.env()), mock.patch.object(app_token, "write_all", side_effect=KeyboardInterrupt):
            with self.assertRaises(SystemExit) as e:
                app_token.main([REAL, "--out", out])
        self.assertNotEqual(e.exception.code, 0)
        self.assertEqual(len(ta.Stub.deletes), 1)
        self.assertIn(TOKEN, ta.Stub.deletes[0][1])
        self.assertEqual(os.listdir(real), [])

    def test_s17_a_mutant_without_the_handler_fails(self):
        path = self.mutant("    except KeyboardInterrupt:", "    except ZeroDivisionError:")
        spec = importlib.util.spec_from_file_location("app_token_mutant2", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        real = self.private_dir()
        ta.Stub.deletes = []
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        with mock.patch.dict(os.environ, self.env()), mock.patch.object(mod, "write_all", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                mod.main([REAL, "--out", os.path.join(real, "tok")])
        self.assertEqual(ta.Stub.deletes, [])


def link_in_parents_real(path):
    sys.path.insert(0, os.path.dirname(HERE))
    import app_token
    return app_token.link_in_parents(path)


if __name__ == "__main__":
    unittest.main()
