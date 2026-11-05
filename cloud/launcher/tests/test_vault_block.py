"""Ask #1427: after the vault's firewall refuses the launcher, every command that needs the vault refuses at once for a set time (S1-S8, U1).

Offline. The vault client is a FAKE `az` script in a temporary directory that is first on the PATH the tests build; no real cloud command runs.
Every subprocess is waited for; the fake `az` that must be slow ends by itself or by the launcher's own process-group kill."""
import importlib.util, io, json, os, re, shutil, stat, subprocess, sys, tempfile, threading, time, unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, LAUNCHER)
import cloud_worker as cw  # noqa: E402

SUB = "00000000-0000-0000-0000-000000000001"
REFUSAL = "ERROR: (Forbidden) Client address is not authorized and caller is not a trusted service. Code: Forbidden Inner error: ForbiddenByFirewall"
BEHAVIOURS = {
    "refuse": 'echo "%s" > /dev/stderr; exit 1' % REFUSAL,
    "slow-refuse": 'sleep 1; echo "%s" > /dev/stderr; exit 1' % REFUSAL,
    "slow-ok": 'sleep 1; echo "config-body"',
    "ok": 'echo "config-body"',
    "missing": 'echo "ERROR: (SecretNotFound) A secret with (name/id) was not found in this key vault." > /dev/stderr; exit 1',
    "login": 'echo "ERROR: Please run az login to setup account." > /dev/stderr; exit 1',
    "malformed": "exit 0",
    "hang": "exec sleep 30",
}


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


REAL = os.path.join(LAUNCHER, "cloud_worker.py")


def load_text(text_path, name):
    """The launcher module made from the text in `text_path`, as if it lived at the real launcher path (its sibling modules are found there)."""
    import types
    module = types.ModuleType(name)
    module.__file__ = REAL
    module.TEXT_PATH = text_path
    sys.modules[name] = module
    with open(text_path) as f:
        exec(compile(f.read(), REAL, "exec"), module.__dict__)
    return module


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class Fake(unittest.TestCase):
    """A scratch tree: a fake az and kubectl first on PATH, a state directory, a call log."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vault-block-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.state = os.path.join(self.dir, "state")
        self.log = os.path.join(self.dir, "az-calls")
        self.mode_file = os.path.join(self.dir, "mode")
        self.script("az", 'echo call >> "$FAKE_LOG"\n. "$FAKE_BEHAVIOUR"')
        self.script("kubectl", 'echo fake-token-value')
        open(self.log, "w").close()
        self.mode("refuse")
        self.env = {"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.dir, "XDG_STATE_HOME": self.state, "FAKE_LOG": self.log,
                    "FAKE_BEHAVIOUR": self.mode_file, "CW_VAULT_SUBSCRIPTION": SUB, "CW_VAULT_NAME": "acme-vault", "CW_KUBECTL": os.path.join(self.bin, "kubectl")}

    def script(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)

    def mode(self, name):
        with open(self.mode_file, "w") as f:
            f.write(BEHAVIOURS[name] + "\n")

    def calls(self):
        with open(self.log) as f:
            return len(f.read().split())

    def block_path(self):
        return os.path.join(self.state, cw.CONFIG_DIR_NAME, "cloud", cw.VAULT_BLOCK_NAME)

    def lock_path(self):
        return os.path.join(self.state, cw.CONFIG_DIR_NAME, "cloud", cw.VAULT_BLOCK_LOCK_NAME)

    def expire(self):
        """The retry time has passed: the file's times move into the past."""
        with open(self.block_path()) as f:
            data = json.load(f)
        data["at"] -= 7200
        data["retry_at"] = data["at"] + 60
        with open(self.block_path(), "w") as f:
            json.dump(data, f)

    def login(self, module=cw, timeout=None):
        capture = None
        if timeout:
            capture = lambda argv, env=None, feed=None: module.capture_command(argv, env=env, feed=feed, timeout=timeout)  # noqa: E731
        return module.cluster_token(dict(self.env), "20m", capture=capture)

    def refused(self, module=cw):
        """Run one login; return the ClusterLogin it raised, or None when it worked."""
        try:
            self.login(module)
        except module.ClusterLogin as err:
            return err
        return None


# ---------------------------------------------------------------- scenarios, written against a module so a mutant can be run through them

def scenario_s2(t, module):
    first = t.refused(module)
    t.assertEqual(first.kind, "admin-unreadable")
    t.assertTrue(os.path.exists(t.block_path()))
    t.assertEqual(t.calls(), 1)
    for _ in range(5):
        err = t.refused(module)
        t.assertEqual(err.kind, "vault-blocked")
        t.assertEqual(len(err.line.splitlines()), 1)
        t.assertIn("refused this machine", err.line)
        t.assertRegex(err.line, r"until \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
    t.assertEqual(t.calls(), 1, "the five later commands made no az call")


def scenario_s3(t, module):
    t.refused(module)
    t.assertEqual(t.calls(), 1)
    t.assertTrue(os.path.exists(t.block_path()), "the refusal made a block")
    t.expire()
    t.mode("ok")
    t.assertIsNone(t.refused(module))   # one probe, success
    t.assertEqual(t.calls(), 2)
    t.assertFalse(os.path.exists(t.block_path()), "a success removes the block")
    t.mode("refuse")
    t.refused(module)
    t.assertEqual(t.calls(), 3)
    t.assertTrue(os.path.exists(t.block_path()))
    t.expire()
    t.refused(module)   # the probe is refused: renewed
    t.assertEqual(t.calls(), 4)
    with open(t.block_path()) as f:
        t.assertGreater(json.load(f)["retry_at"], time.time(), "a refusal renews the block")
    t.assertEqual(t.refused(module).kind, "vault-blocked")
    t.assertEqual(t.calls(), 4)


def scenario_s6(t, module):
    t.mode("slow-refuse")
    code = ("import sys; sys.path.insert(0, %r)\nimport importlib.util\n"
            "import types\nm = types.ModuleType('mod'); m.__file__ = %r; sys.modules['mod'] = m\nexec(compile(open(%r).read(), %r, 'exec'), m.__dict__)\n"
            "import os\ntry:\n    m.cluster_token(dict(os.environ), '20m')\nexcept m.ClusterLogin as e:\n    print(e.kind)\n") % (LAUNCHER, REAL, getattr(module, "TEXT_PATH", REAL), REAL)
    env = dict(t.env)
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) for _ in range(10)]
    outs = []
    try:
        for p in procs:
            outs.append(p.communicate(timeout=60)[0].strip())
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
            p.wait()
    t.assertLessEqual(t.calls(), 1, "ten concurrent commands: at most one az call")
    t.assertEqual(sorted(outs).count("vault-blocked"), 9)


class Scenarios(Fake):
    def test_s2_the_first_refusal_makes_the_block_and_the_next_five_make_no_call(self):
        scenario_s2(self, cw)

    def test_s2_the_block_file_is_private_small_and_holds_no_secret_address_or_name(self):
        self.refused()
        info = os.stat(self.block_path())
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertLess(info.st_size, 400)
        with open(self.block_path()) as f:
            text = f.read()
        for word in (SUB, "acme-vault", "config-body"):
            self.assertNotIn(word, text)
        self.assertEqual(set(json.loads(text)), {"at", "reason", "retry_at"})
        data = json.loads(text)
        self.assertAlmostEqual(data["retry_at"] - data["at"], 600, delta=1)   # ten minutes by default
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.block_path())) if n.endswith(".tmp")], [])   # written by rename

    def test_s2_the_setting_sets_the_length_between_one_minute_and_one_hour(self):
        for value, minutes in (("1m", 1), ("45m", 45), ("1h", 60), ("0m", 10), ("2h", 10), ("junk", 10)):
            with self.subTest(value=value):
                if os.path.exists(self.block_path()):
                    os.unlink(self.block_path())
                self.env["CW_VAULT_BLOCK_" + "DURATION"] = value
                self.refused()
                with open(self.block_path()) as f:
                    data = json.load(f)
                self.assertAlmostEqual(data["retry_at"] - data["at"], 60 * minutes, delta=1)

    def test_s2_the_wiring_refuses_with_the_one_line_and_the_same_refusal_type(self):
        wiring = load(os.path.join(ROOT, "bin", "cloud_worker.py"), "cw_wiring_vault_block")
        self.refused()
        calls = self.calls()
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(wiring.Refusal) as caught:
                wiring.cluster_token()
        self.assertEqual(len(str(caught.exception).splitlines()), 1)
        self.assertIn("refused this machine", str(caught.exception))
        self.assertEqual(self.calls(), calls)

    def test_s3_after_the_time_one_probe_success_clears_refusal_renews(self):
        scenario_s3(self, cw)

    def test_s3_a_second_command_during_the_probe_starts_no_call(self):
        self.refused()
        self.expire()
        self.mode("slow-ok")
        result = []
        probe = threading.Thread(target=lambda: result.append(self.refused()))
        probe.start()
        try:
            end = time.time() + 20
            while not os.path.exists(self.lock_path()) and time.time() < end:
                time.sleep(0.02)
            self.assertTrue(os.path.exists(self.lock_path()))
            second = self.refused()
        finally:
            probe.join(60)
        self.assertEqual(second.kind, "vault-blocked")
        self.assertEqual(self.calls(), 2)   # the first refusal and the one probe
        self.assertEqual(result, [None])
        self.assertFalse(os.path.exists(self.lock_path()), "the lock is released")

    def test_s3_a_stale_lock_is_ignored_and_a_fresh_one_is_not(self):
        self.refused()
        self.expire()
        self.mode("ok")
        os.makedirs(os.path.dirname(self.lock_path()), exist_ok=True)
        open(self.lock_path(), "w").close()
        self.assertEqual(self.refused().kind, "vault-blocked")   # a fresh lock: a probe is running
        self.assertEqual(self.calls(), 1)
        old = time.time() - cw.vault_lock_stale_seconds() - 100
        os.utime(self.lock_path(), (old, old))
        self.assertIsNone(self.refused())
        self.assertEqual(self.calls(), 2)

    def test_s3_a_lock_of_a_killed_command_is_ignored_at_once(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        os.makedirs(os.path.dirname(self.lock_path()), exist_ok=True)
        with open(self.lock_path(), "w") as f:
            f.write(str(dead.pid))
        self.mode("ok")
        self.assertIsNone(self.refused())
        self.assertEqual(self.calls(), 1)
        self.assertFalse(os.path.exists(self.lock_path()))

    def test_s6_ten_concurrent_commands_make_at_most_one_call(self):
        scenario_s6(self, cw)

    def test_s5_the_reaper_pass_while_blocked_makes_no_call_and_logs_one_line(self):
        self.refused()
        self.assertEqual(self.calls(), 1)
        runs, lines = [], io.StringIO()

        class Stop(BaseException):
            pass

        class Refresher:
            notices = []

            def run(self):
                runs.append(1)
                raise AssertionError("the reaper asked for a login while blocked")

        def sleep(seconds):
            raise Stop()

        args = mock.Mock(loop=60)
        env = dict(self.env, CW_CLOUD_NAMESPACE="vault-block-test")
        with mock.patch.object(cw, "_lock_root", return_value=os.path.join(self.dir, "locks")), redirect_stderr(lines):
            with self.assertRaises(Stop):
                cw._reap_main(env, args, sleep, refresher=Refresher())
        skipped = [l for l in lines.getvalue().splitlines() if "reaper pass skipped" in l]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(runs, [])
        self.assertEqual(self.calls(), 1)
        # when the block is gone the pass works as before: the login refresh runs
        os.unlink(self.block_path())
        seen = []

        class Refresher2(Refresher):
            def run(self):
                seen.append(1)
                return cw.TokenRefresh("fresh")

        with mock.patch.object(cw, "_lock_root", return_value=os.path.join(self.dir, "locks")), mock.patch.object(cw, "Kube", side_effect=cw.CloudError("no login", 2)), \
                redirect_stderr(io.StringIO()), mock.patch("builtins.print"):
            with self.assertRaises(Stop):
                cw._reap_main(env, args, sleep, refresher=Refresher2())
        self.assertEqual(seen, [1])

    def test_s4_no_other_failure_makes_a_block(self):
        cases = []
        for behaviour in ("hang", "missing", "login", "malformed"):
            cases.append(behaviour)
        for behaviour in cases:
            with self.subTest(behaviour=behaviour):
                self.mode(behaviour)
                err = self.refused_with(1 if behaviour == "hang" else None)
                self.assertEqual(err.kind, "admin-unreadable")
                self.assertFalse(os.path.exists(self.block_path()))
                self.assertFalse(os.path.exists(self.lock_path()))
        self.assertGreaterEqual(self.calls(), 4)   # each case did reach the vault
        # a failure before the vault: no vault name
        before = self.calls()
        self.env.pop("CW_VAULT_NAME")
        with self.assertRaises(cw.ClusterLogin) as caught:
            self.login()
        self.assertEqual(caught.exception.kind, "vault-settings-missing")
        self.assertEqual(self.calls(), before)
        self.assertFalse(os.path.exists(self.block_path()))

    def refused_with(self, timeout):
        try:
            self.login(timeout=timeout)
        except cw.ClusterLogin as err:
            return err
        self.fail("the login worked")

    def test_u1_a_vault_that_answers_writes_no_block(self):
        self.mode("ok")
        self.assertEqual(self.login(), "fake-token-value")
        self.assertFalse(os.path.exists(self.block_path()))
        self.assertFalse(os.path.exists(self.lock_path()))
        self.assertEqual(self.calls(), 1)

    def test_a_recovered_refusal_makes_no_block(self):
        """The launch's recovery (the vault access check) fixed it: the second read answers, so no block."""
        answers = iter([(1, "", REFUSAL), (0, "config-body\n", "")])
        reads = []

        def capture(argv, env=None, feed=None):
            if argv[0] == "az":
                reads.append(1)
                return next(answers)
            return (0, "fake-token-value\n", "")

        def recovery(read, env):
            return read()

        self.assertEqual(cw.cluster_token(dict(self.env), "20m", capture=capture, recovery=recovery), "fake-token-value")
        self.assertEqual(len(reads), 2)
        self.assertFalse(os.path.exists(self.block_path()))


# ---------------------------------------------------------------- S7: a mutant that never writes the block

class Mutant(Fake):
    def mutant(self):
        source = read("cloud/launcher/cloud_worker.py")
        old = '''        os.makedirs(directory, mode=0o700, exist_ok=True)
        body = json.dumps('''
        new = '''        return
        body = json.dumps('''
        self.assertEqual(source.count(old), 1)
        mutated = source.replace(old, new)
        self.assertNotEqual(mutated, source, "the mutation is applied")
        path = os.path.join(self.dir, "cloud_worker_mutant.py")
        with open(path, "w") as f:
            f.write(mutated)
        return load_text(path, "cw_mutant_vault_block")

    def test_s7_the_mutant_with_no_block_fails_s2_s3_and_s6(self):
        mutant = self.mutant()
        self.assertTrue(callable(mutant.write_vault_block))
        for name, scenario in (("s2", scenario_s2), ("s3", scenario_s3), ("s6", scenario_s6)):
            with self.subTest(scenario=name):
                self.setUp()   # a clean scratch tree for each
                with self.assertRaises(AssertionError):
                    scenario(self, mutant)
        # the real module passes the same scenarios in the same harness
        for name, scenario in (("s2", scenario_s2), ("s3", scenario_s3), ("s6", scenario_s6)):
            with self.subTest(real=name):
                self.setUp()
                scenario(self, cw)


# ---------------------------------------------------------------- S1 and S8: the documents

README, INTERFACE = "cloud/launcher/README.md", "docs/cloud-worker-interface.md"
HEAD = "## The vault block (ask #1427)"


def section(text):
    return text.split(HEAD)[1].split("\n## ")[0]


CAUSE = {   # S1: the cause and the three callers
    "every caller started its own vault call": r"started its own `az keyvault secret show`",
    "about ten in flight": r"about ten vault calls were in flight",
    "tens of seconds each": r"tens of seconds to fail",
    "repeated for hours": r"repeated for hours",
    "the launch": r"cluster launch",
    "the reaper loop": r"each pass of the reaper loop",
    "the token mint": r"each token mint",
}
FIVE = {   # S8: what, where, how long, the setting, how to clear it
    "what the block is": r"one small private file holding the time, a one-line reason and the retry time",
    "where it lives": r"`vault-block\.json` in the switchboard's state directory",
    "how long": r"ten minutes by default",
    "the setting": r"`CW_VAULT_BLOCK_DURATION`",
    "how to clear it": r"delete the file `vault-block\.json`",
}


def shows(text, table):
    return [k for k, rx in table.items() if re.search(rx, text)]


class Docs(unittest.TestCase):
    def test_s1_the_readme_states_the_cause_and_names_the_three_callers(self):
        self.assertEqual(shows(section(read(README)), CAUSE), list(CAUSE))
        self.assertLess(section(read(README)).index("Cause:"), section(read(README)).index("What the block is:"))

    def test_s1_a_section_without_the_reaper_loop_shows_fewer(self):
        text = section(read(README)).replace("each pass of the reaper loop", "each pass")
        self.assertEqual(len(shows(text, CAUSE)), len(CAUSE) - 1)

    def test_s8_both_documents_say_the_five_things(self):
        for rel in (README, INTERFACE):
            with self.subTest(document=rel):
                self.assertEqual(shows(section(read(rel)), FIVE), list(FIVE))

    def test_s8_a_document_without_how_to_clear_it_shows_four(self):
        text = section(read(README)).replace("delete the file `vault-block.json`", "wait")
        self.assertEqual(len(shows(text, FIVE)), 4)

    def test_the_names_in_the_documents_are_the_code_s(self):
        self.assertEqual(cw.VAULT_BLOCK_NAME, "vault-block.json")
        self.assertEqual(cw.VAULT_BLOCK_SETTING, "CW_VAULT_BLOCK_DURATION")
        self.assertEqual(cw.VAULT_BLOCK_DEFAULT_MINUTES, 10)


class FailClosed(Fake):
    """Plan S11 and cos's rule: a block file that cannot be trusted BLOCKS (one clear line naming the file, no vault call, no uncaught error)
    until the file's own time plus the setting's length; then the single probe runs."""

    def put(self, body, mode="w"):
        os.makedirs(os.path.dirname(self.block_path()), mode=0o700, exist_ok=True)
        with open(self.block_path(), mode) as f:
            f.write(body)

    def assertBlocksClosed(self, label):
        err = self.refused()
        self.assertIsNotNone(err, label)
        self.assertEqual(err.kind, "vault-blocked", label)
        self.assertEqual(len(err.line.splitlines()), 1, label)
        self.assertIn(self.block_path(), err.line, label)
        self.assertIn("delete that file to clear it by hand", err.line, label)
        self.assertEqual(self.calls(), 0, label)
        return err

    def damaged_files(self):
        good = json.dumps({"at": time.time(), "reason": "x", "retry_at": time.time() + 600})
        def symlink():
            os.makedirs(os.path.dirname(self.block_path()), exist_ok=True)
            target = os.path.join(self.dir, "elsewhere.json")
            with open(target, "w") as f:
                f.write(good)
            os.symlink(target, self.block_path())
        return {
            "corrupt text": lambda: self.put("{not json"),
            "empty": lambda: self.put(""),
            "huge": lambda: self.put(good + " " * 70000),
            "negative retry": lambda: self.put('{"at": 1, "retry_at": -5}'),
            "NaN retry": lambda: self.put('{"at": 1, "retry_at": NaN}'),
            "text retry": lambda: self.put('{"at": 1, "retry_at": "soon"}'),
            "boolean retry": lambda: self.put('{"at": 1, "retry_at": true}'),
            "symlink": symlink,
        }

    def test_each_untrustworthy_file_blocks_with_one_line_and_no_call(self):
        for label, make in self.damaged_files().items():
            with self.subTest(label):
                if os.path.lexists(self.block_path()):
                    os.unlink(self.block_path())
                make()
                self.assertBlocksClosed(label)

    def test_a_far_future_or_huge_retry_time_is_capped_and_never_crashes(self):
        for label, retry in (("30 years", time.time() + 30 * 365 * 86400), ("1e12", 1e12), ("1e300", 1e300)):
            with self.subTest(label):
                self.put(json.dumps({"at": 1e12, "reason": "x", "retry_at": retry}))
                err = self.refused()
                self.assertEqual(err.kind, "vault-blocked")
                self.assertEqual(self.calls(), 0)
                block = cw.read_vault_block(self.env)
                self.assertLessEqual(block[1], time.time() + 60 * 60 + 5)

    def test_a_future_dated_file_is_blocked_only_until_its_real_time_plus_the_setting(self):
        self.put("{not json")
        future = time.time() + 10 * 365 * 86400
        os.utime(self.block_path(), (future, future))   # the change time stays the real one
        self.assertBlocksClosed("future-dated")
        self.assertLessEqual(cw.read_vault_block(self.env)[1], time.time() + 60 * cw.vault_block_minutes(self.env) + 5)

    def test_the_end_of_the_block_of_a_corrupt_file_runs_the_single_probe(self):
        self.put("{not json")
        self.assertBlocksClosed("corrupt")
        old = time.time() - 3600
        os.utime(self.block_path(), (old, old))
        self.mode("ok")
        self.assertIsNone(self.refused())   # the probe ran and succeeded
        self.assertEqual(self.calls(), 1)
        self.assertFalse(os.path.exists(self.block_path()), "success removes the file")

    def test_the_end_of_the_block_of_a_corrupt_file_renews_it_on_a_refusal(self):
        self.put("x" * 70000)
        old = time.time() - 3600
        os.utime(self.block_path(), (old, old))
        self.assertEqual(self.refused().kind, "admin-unreadable")
        self.assertEqual(self.calls(), 1)
        self.assertIsNotNone(cw.read_vault_block(self.env)[1])
        self.assertIsNone(cw.read_vault_block(self.env)[2], "rewritten as a good block")
        self.assertEqual(self.refused().kind, "vault-blocked")
        self.assertEqual(self.calls(), 1)

    def test_a_symlink_block_is_not_followed_and_is_replaced_by_the_renewal(self):
        self.damaged_files()["symlink"]()
        self.assertBlocksClosed("symlink")
        old = time.time() - 3600
        os.utime(self.block_path(), (old, old), follow_symlinks=False)
        self.refused()
        self.assertFalse(os.path.islink(self.block_path()))
        self.assertEqual(self.calls(), 1)


class ProbeLock(Fake):
    """Plan S12: the probe lock never hangs or spins, whatever it is; a block never outlasts the setting measured from now."""

    CHILD = ("import sys, os, time, types\nsys.path.insert(0, %r)\nm = types.ModuleType('mod'); m.__file__ = %r; sys.modules['mod'] = m\n"
             "exec(compile(open(%r).read(), %r, 'exec'), m.__dict__)\nt = time.time()\n"
             "try:\n    m.cluster_token(dict(os.environ), '20m')\n    kind = 'ok'\nexcept m.ClusterLogin as e:\n    kind = e.kind\n"
             "print('%%s %%.2f' %% (kind, time.time() - t))\n")

    def run_child(self, module_text=REAL):
        """Run one command in a child with a 10 second limit; return (seconds, stderr) or (None, ...) when it had to be killed."""
        code = self.CHILD % (LAUNCHER, REAL, module_text, REAL)
        proc = subprocess.Popen([sys.executable, "-c", code], env=dict(self.env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            return None, err
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        return float(out.split()[1]), err

    def make_lock(self, kind):
        os.makedirs(os.path.dirname(self.lock_path()), mode=0o700, exist_ok=True)
        lock = self.lock_path()
        if kind == "fifo":
            os.mkfifo(lock)
        elif kind == "symlink":
            os.symlink(os.path.join(self.dir, "nowhere"), lock)
        elif kind == "directory":
            os.mkdir(lock)
            old = time.time() - 3600
            os.utime(lock, (old, old))
        else:   # a regular file of a process that is gone
            with open(lock, "w") as f:
                f.write("999999999")

    def check_case(self, kind, module_text=REAL):
        self.make_lock(kind)
        seconds, err = self.run_child(module_text)
        self.assertIsNotNone(seconds, "%s: the command hung (killed after 10 s)" % kind)
        self.assertLess(seconds, 5, kind)
        self.assertLessEqual(self.calls(), 1, kind)
        return err

    def test_each_lock_case_returns_at_once_with_at_most_one_call(self):
        for kind in ("fifo", "symlink", "directory", "dead"):
            with self.subTest(kind):
                self.setUp()
                err = self.check_case(kind)
                lines = [l for l in err.splitlines() if l.strip()]
                if kind == "dead":
                    self.assertEqual(lines, [], "a dead holder's lock is ignored silently")
                else:
                    self.assertEqual(len(lines), 1, err)
                    self.assertIn(self.lock_path(), lines[0])
                    self.assertIn("delete it by hand", lines[0])

    def test_a_mutant_that_opens_the_lock_blocking_hangs_on_the_fifo(self):
        source = read("cloud/launcher/cloud_worker.py")
        old = "os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)"
        self.assertEqual(source.count(old), 1)
        mutated = source.replace(old, "os.open(lock, os.O_RDONLY)")
        self.assertNotEqual(mutated, source, "the mutation is applied")
        path = os.path.join(self.dir, "cloud_worker_mutant_lock.py")
        with open(path, "w") as f:
            f.write(mutated)
        self.make_lock("fifo")
        seconds, _ = self.run_child(path)
        self.assertIsNone(seconds, "the blocking open hangs")

    def test_a_clock_set_back_a_year_never_lengthens_the_block(self):
        self.refused()
        self.assertEqual(self.calls(), 1)
        year = 365 * 86400
        fake = lambda: time.time() - year   # noqa: E731
        minutes = cw.vault_block_minutes(self.env)
        block = cw.read_vault_block(self.env, fake())
        self.assertLessEqual(block[1], fake() + 60 * minutes + 5)
        with self.assertRaises(cw.ClusterLogin) as caught:
            cw.VaultFlight.enter(dict(self.env), clock=fake)
        self.assertEqual(caught.exception.kind, "vault-blocked")
        later = lambda: time.time() - year + 60 * minutes + 30   # noqa: E731
        flight = cw.VaultFlight.enter(dict(self.env), clock=later)   # the one probe
        flight.release()
        self.assertEqual(self.calls(), 1)


class Isolation(Fake):
    """The block follows the state directory the tests redirect (XDG_STATE_HOME) and nowhere else."""

    def test_a_firewall_refusal_writes_the_block_there_and_nothing_under_another_home(self):
        other_home = tempfile.mkdtemp(prefix="vault-block-home-")
        self.addCleanup(shutil.rmtree, other_home, True)
        self.env["HOME"] = other_home
        self.assertEqual(self.refused().kind, "admin-unreadable")
        self.assertTrue(os.path.exists(self.block_path()))
        self.assertTrue(self.block_path().startswith(self.state + os.sep))
        self.assertEqual(os.listdir(other_home), [], "nothing under the default state directory's home")

    def test_a_block_left_in_one_state_directory_does_not_touch_another(self):
        self.refused()
        self.assertTrue(os.path.exists(self.block_path()))
        second = tempfile.mkdtemp(prefix="vault-block-other-")
        self.addCleanup(shutil.rmtree, second, True)
        self.env["XDG_STATE_HOME"] = os.path.join(second, "state")
        self.mode("ok")
        self.assertIsNone(self.refused())
        self.assertEqual(self.calls(), 2)


class Setting(unittest.TestCase):
    """The duration's own rule is the existing duration kind: 1m to 60m (its row is a separate commit of the ask)."""

    def test_the_existing_duration_rule_holds_one_to_sixty_minutes(self):
        for good in ("1m", "10m", "60m", "1h"):
            self.assertTrue(cw._duration_ok(good), good)
        for bad in ("0m", "61m", "2h", "-5m", "10", "m", "10s"):
            self.assertFalse(cw._duration_ok(bad), bad)


if __name__ == "__main__":
    unittest.main()
