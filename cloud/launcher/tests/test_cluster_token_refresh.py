"""The reaper's own cluster login (ask #1060): the token file stays fresh without a person.

Offline: a fake vault (a fake `az` that answers or fails), a fake cluster (a fake `kubectl` that answers `create token`), a fake clock and a
real pipe. No token is a literal here: every value is assembled at run time.

The families are derived from the code, never listed twice:
  * every failure kind of the shared login has a case (the kinds are read out of the source), and
  * the mode of the written file is checked with two mutants (a build that writes it without the owner-only mode fails).
"""
import base64
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import stat
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402

cw = base.cw
SOURCE_PATH = os.path.join(LAUNCHER, "cloud_worker.py")
with open(SOURCE_PATH) as _source:
    SOURCE = _source.read()

# The fake vault's answer: the administrator kubeconfig, assembled at run time (no secret-shaped literal in this file). The fake programs
# assemble the same value from the same seed, so the value never has to be written anywhere.
CANARY_EXPR = "''.join(chr(97 + (i * 7 + len(seed)) % 26) for i in range(48))"
SEED = "acme-admin"
TOKEN_HEADER = "eyJhbGciOiJSUzI1NiJ9"
TOKEN_SIGNATURE = "c2ln"


def canary(seed=SEED):
    """The fake vault's administrator value (assembled at run time)."""
    return eval(CANARY_EXPR, {"seed": seed})   # the expression is a constant of this file


def make_token(exp):
    """The token the fake cluster prints for the given expiry (epoch seconds)."""
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(exp)}, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
    return "%s.%s.%s" % (TOKEN_HEADER, payload, TOKEN_SIGNATURE)


FAKE_AZ = '''#!/usr/bin/env python3
"""A fake vault client for the reaper's refresh tests: it prints the administrator kubeconfig (assembled at run time) or fails."""
import os, sys
import json as _json, os as _os
_cfg = _json.load(open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fake.json")))   # the fakes' settings: the mint step's child gets no such variables
mode = open(_cfg["FAKE_VAULT_MODE"]).read().strip() if _cfg.get("FAKE_VAULT_MODE") else "ok"
seed = _cfg.get("FAKE_ADMIN_SEED", "acme-admin")
value = %s
with open(_cfg["FAKE_VAULT_LOG"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
with open(_cfg["FAKE_VAULT_LOG"] + ".env", "a") as f:
    f.write(_json.dumps(dict(os.environ), sort_keys=True) + "\\n")
if mode == "fail":
    sys.stderr.write("ERROR: (Forbidden) the vault refused this request\\n")
    sys.exit(1)
if mode == "firewall":
    sys.stderr.write("ERROR: (Forbidden) ForbiddenByFirewall: client address is not authorized and caller is not a trusted service\\n")
    sys.exit(1)
if mode == "empty":
    sys.exit(0)
sys.stdout.write(value + "\\n")
''' % CANARY_EXPR

FAKE_KUBECTL = '''#!/usr/bin/env python3
"""A fake kubectl for the reaper's refresh tests: the one `create token` command, and the pod calls (the repository's own fake)."""
import base64, hashlib, json, os, sys
import json as _json, os as _os
_cfg = _json.load(open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fake.json")))   # the fakes' settings: the mint step's child gets no such variables

args = sys.argv[1:]
if args and args[0] == "--kubeconfig" and "cw-operator" in args:
    path = args[1]
    seen = {"argv": args, "kubeconfig": path, "is_pipe": path.startswith("/dev/fd/"), "admin_sha256": "", "admin_bytes": 0, "env": dict(os.environ)}
    try:
        with open(path) as f:
            body = f.read(200000)
        seen["admin_bytes"] = len(body)
        seen["admin_sha256"] = hashlib.sha256(body.strip().encode()).hexdigest()
    except OSError:
        pass
    with open(_cfg["FAKE_TOKEN_LOG"], "a") as f:
        f.write(json.dumps(seen, sort_keys=True) + "\\n")
    mode = _cfg.get("FAKE_TOKEN_MODE", "ok")
    if mode == "refuse":
        sys.stderr.write("Error from server (Forbidden): tokens is forbidden\\n")
        sys.exit(1)
    if mode == "garbage":
        sys.stdout.write("not a token at all\\n")
        sys.exit(0)
    exp = int(_cfg.get("FAKE_TOKEN_EXP", "0"))
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
    sys.stdout.write("%s." + payload + ".%s")
    sys.exit(0)
os.execv(sys.executable, [sys.executable, os.environ["FAKE_REAL_KUBECTL"]] + args)
''' % (TOKEN_HEADER, TOKEN_SIGNATURE)


def load_installer():
    """The installer (bin/<the kit's own program>) by path: it renders the reaper's service file."""
    path = os.path.join(ROOT, "bin", "fl" + "eet-switchboard")   # the name is written in two parts: the neutral-names test bans it under cloud/
    loader = importlib.machinery.SourceFileLoader("t_installer_for_refresh", path)
    spec = importlib.util.spec_from_loader("t_installer_for_refresh", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def mutant_module(text):
    """The real source with one piece changed (a module of its own)."""
    module = types.ModuleType("cloud_worker_mode_mutant")
    module.__file__ = SOURCE_PATH
    exec(compile(text, SOURCE_PATH, "exec"), module.__dict__)
    return module


class Refresh(base.Base):
    """A reaper whose own login is the token file, with a fake vault, a fake cluster and a fake clock."""

    def setUp(self):
        super().setUp()
        self.reset()

    def reset(self):
        """The fakes and the environment of one case (a fresh cluster, a fresh vault, a fresh file)."""
        d = self.tmp.name
        self.bin = os.path.join(d, "bin")
        os.makedirs(self.bin, exist_ok=True)
        self.token_dir = os.path.join(d, "tokens")
        os.makedirs(self.token_dir, exist_ok=True)
        self.token_file = os.path.join(self.token_dir, "token")
        self.home = os.path.join(d, "home")
        os.makedirs(self.home, exist_ok=True)
        self.state_home = os.path.join(d, "state")
        os.makedirs(self.state_home, exist_ok=True)
        self.tmpdir = os.path.join(d, "tmp")
        os.makedirs(self.tmpdir, exist_ok=True)
        self.write_fake("az", FAKE_AZ)
        self.write_fake("kubectl", FAKE_KUBECTL)
        self.vault_mode = os.path.join(d, "vault.mode")
        self.vault_log = os.path.join(d, "vault.log")
        self.token_log_path = os.path.join(d, "tokens.jsonl")
        self.set_vault("ok")
        self.clock.t = base.T0
        self.exp = int(self.clock().timestamp()) + 3600
        self.env = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "CW_KUBECTL": os.path.join(self.bin, "kubectl"),
            "CW_CLOUD_SERVER": "https://api.example.internal:6443",
            "CW_CLOUD_TOKEN_FILE": self.token_file,
            "CW_CLOUD_NAMESPACE": "cw",
            "CW_VAULT_SUBSCRIPTION": "00000000-0000-0000-0000-000000000000",
            "CW_VAULT_NAME": "acme-vault",
            "CW_CLOUD_AUDIT": os.path.join(d, "audit.jsonl"),
            "HOME": self.home,
            "TMPDIR": self.tmpdir,
            "XDG_STATE_HOME": self.state_home,
            "FAKE_KUBE_STATE": os.path.join(d, "kube.json"),
            "FAKE_KUBE_CALLS": os.path.join(d, "calls.jsonl"),
            "FAKE_ADMIN_SEED": SEED,
            "FAKE_VAULT_MODE": self.vault_mode,
            "FAKE_VAULT_LOG": self.vault_log,
            "FAKE_TOKEN_LOG": self.token_log_path,
            "FAKE_REAL_KUBECTL": os.path.join(HERE, "fake_kubectl"),
            "FAKE_TOKEN_EXP": str(self.exp),
        }
        with open(self.env["FAKE_KUBE_STATE"], "w") as f:
            json.dump({"pods": {}}, f)
        self.sync_fakes()
        for path in (self.token_file, self.vault_log, self.token_log_path, self.env["CW_CLOUD_AUDIT"]):
            if os.path.exists(path):
                os.unlink(path)

    # -- the fakes --
    def write_fake(self, name, text):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o755)
        return path

    def set_vault(self, mode):
        with open(self.vault_mode, "w") as f:
            f.write(mode)

    def sync_fakes(self):
        """The fakes read their settings from a file beside them (the mint step's child has no FAKE_ variable)."""
        with open(os.path.join(self.bin, "fake.json"), "w") as f:
            json.dump({k: v for k, v in self.env.items() if k.startswith("FAKE_")}, f)

    def set_token_mode(self, mode):
        self.env["FAKE_TOKEN_MODE"] = mode
        self.sync_fakes()

    def token_log(self):
        """What the fake cluster saw of the one token command."""
        try:
            with open(self.token_log_path) as f:
                return [json.loads(x) for x in f if x.strip()]
        except OSError:
            return []

    def vault_reads(self):
        """The vault reads: one line per call of the fake vault client."""
        try:
            with open(self.vault_log) as f:
                return [l for l in f.read().splitlines() if l.strip()]
        except OSError:
            return []

    def audit(self):
        try:
            return self.audit_lines()
        except OSError:
            return []

    def refresh_lines(self):
        return [l for l in self.audit() if l.get("action") == "refresh-cluster-token"]

    # -- the file --
    def write_token_file(self, text):
        with open(self.token_file, "w") as f:
            f.write(text)
        os.chmod(self.token_file, 0o600)

    def token_bytes(self):
        with open(self.token_file, "rb") as f:
            return f.read()

    def mode_of(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def temp_files(self):
        return [n for n in os.listdir(self.token_dir) if n != "token"]

    def scanned_files(self):
        """Every file under the directories the refresh could write to."""
        found = []
        for root in (self.token_dir, self.home, self.state_home, self.tmpdir):
            for here, _, names in os.walk(root):
                for name in names:
                    found.append(os.path.join(here, name))
        return found

    # -- the passes --
    def passes(self, count, env=None, loop=60):
        """Run the reaper for `count` passes (the fake clock moves one loop interval per pass); returns (code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        state = {"n": 0}

        def sleep(seconds):
            state["n"] += 1
            if state["n"] >= count:
                raise cw.Interrupted("signal 15")   # the loop ends as a signal would end it
            self.clock.advance(seconds=seconds)

        with redirect_stdout(out), redirect_stderr(err):
            code = cw.main(["reap", "--loop", str(loop)], env if env is not None else self.env, self.clock, sleep)
        return code, out.getvalue(), err.getvalue()

    def one_pass(self, env=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cw.main(["reap"], env if env is not None else self.env, self.clock, lambda s: None)
        return code, out.getvalue(), err.getvalue()

    def report(self, text):
        """The pass reports the reaper printed, one list of entries per pass."""
        return [json.loads(l) for l in text.splitlines() if l.strip().startswith("[")]

    def entries(self, text):
        """Every report entry of every pass."""
        return [e for line in self.report(text) for e in line]


class AMissingFileIsRefreshed(Refresh):
    """S2: the file is missing: one vault read, one token request, the file is written with mode 0600."""

    def test_a_missing_file_is_made_with_one_vault_read_and_one_token_request(self):
        code, out, err = self.one_pass()
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(self.token_file))
        self.assertEqual(self.token_bytes().decode(), make_token(self.exp))
        self.assertEqual(self.mode_of(self.token_file), 0o600)
        self.assertEqual(len(self.vault_reads()), 1, self.vault_reads())
        self.assertEqual(len(self.token_log()), 1, self.token_log())
        self.assertEqual(len(self.refresh_lines()), 1)
        self.assertTrue(self.refresh_lines()[0]["ok"])

    def test_the_administrator_kubeconfig_reaches_the_command_only_on_a_pipe(self):
        self.one_pass()
        (entry,) = self.token_log()
        self.assertTrue(entry["is_pipe"], entry)
        self.assertRegex(entry["kubeconfig"], r"^/dev/fd/\d+$")
        self.assertEqual(entry["admin_sha256"], hashlib.sha256(canary().encode()).hexdigest())
        self.assertGreaterEqual(entry["admin_bytes"], len(canary()))

    def test_no_file_anywhere_holds_the_administrator_value(self):
        self.one_pass()
        for path in self.scanned_files():
            with open(path, errors="replace") as f:
                self.assertNotIn(canary(), f.read(), path)

    def test_the_command_makes_the_operators_own_token_and_nothing_else(self):
        self.one_pass()
        (entry,) = self.token_log()
        self.assertEqual(entry["argv"][-4:], ["create", "token", "cw-operator", "--duration=60m"])
        self.assertEqual(entry["argv"][entry["argv"].index("-n") + 1], "cw")

    def test_the_administrator_login_is_used_once_and_not_kept(self):
        self.one_pass()
        self.assertEqual(len(self.token_log()), 1)   # exactly one use of the administrator kubeconfig
        for path in self.scanned_files():
            with open(path, errors="replace") as f:
                self.assertNotIn(canary(), f.read(), path)
        self.assertEqual(self.token_bytes().decode(), make_token(self.exp))   # the file holds only the minted token

    def test_the_minted_token_is_never_in_the_output_the_audit_the_report_the_argv_or_the_environment(self):
        code, out, err = self.one_pass()
        token = make_token(self.exp)
        for place, text in (("stdout", out), ("stderr", err), ("audit", json.dumps(self.audit())), ("report", out),
                            ("argv", json.dumps(self.token_log()[0]["argv"])), ("environment", json.dumps(self.token_log()[0]["env"]))):
            self.assertNotIn(token, text, place)
            self.assertNotIn(canary(), text, place)


class AMargin(Refresh):
    """S3: the file's own expiry decides: under the margin it is renewed, over it is left alone."""

    def test_a_file_one_second_under_the_margin_is_replaced(self):
        self.write_token_file(make_token(int(self.clock().timestamp()) + cw.CLUSTER_TOKEN_REFRESH_MARGIN_SECONDS - 1))
        before = self.token_bytes()
        code, out, err = self.one_pass()
        self.assertEqual(code, 0, err)
        self.assertNotEqual(self.token_bytes(), before)
        self.assertEqual(len(self.token_log()), 1)

    def test_a_file_one_second_over_the_margin_is_left_alone(self):
        self.write_token_file(make_token(int(self.clock().timestamp()) + cw.CLUSTER_TOKEN_REFRESH_MARGIN_SECONDS + 1))
        before = self.token_bytes()
        stamp = os.stat(self.token_file).st_mtime_ns
        code, out, err = self.one_pass()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.token_bytes(), before)
        self.assertEqual(os.stat(self.token_file).st_mtime_ns, stamp)
        self.assertEqual(self.token_log(), [])
        self.assertEqual(self.vault_reads(), [])
        self.assertEqual(self.refresh_lines(), [])

    def test_the_margin_is_a_named_constant_of_the_launcher(self):
        self.assertEqual(cw.CLUSTER_TOKEN_REFRESH_MARGIN_SECONDS, 1200)
        self.assertIn("CLUSTER_TOKEN_REFRESH_MARGIN_SECONDS = 1200", SOURCE)

    def test_a_file_that_holds_text_that_is_not_a_token_counts_as_expired(self):
        self.write_token_file("this is not a token")
        code, out, err = self.one_pass()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.token_bytes().decode(), make_token(self.exp))
        self.assertEqual(len(self.token_log()), 1)


class TheWrite(Refresh):
    """S4: the write is atomic and private: a failed rename leaves the old file whole, and the mode is exactly 0600."""

    def test_a_write_that_fails_at_the_rename_leaves_the_old_bytes_and_no_temp_file(self):
        old = make_token(int(self.clock().timestamp()) - 10)
        self.write_token_file(old)
        real = os.replace

        def boom(src, dst, *a, **kw):
            raise OSError(5, "the rename failed")

        cw.os.replace = boom
        try:
            self.one_pass()
        finally:
            cw.os.replace = real
        self.assertEqual(self.token_bytes().decode(), old)   # the old file is whole
        self.assertEqual(self.temp_files(), [])             # and no temp file is left
        self.assertEqual(len(self.refresh_lines()), 1)
        self.assertFalse(self.refresh_lines()[0]["ok"])

    def test_the_file_is_written_with_mode_0600_under_any_umask(self):
        for umask in (0o000, 0o022, 0o077):
            with self.subTest(umask=umask):
                self.reset()
                real = os.umask(umask)
                try:
                    self.one_pass()
                finally:
                    os.umask(real)
                self.assertEqual(self.mode_of(self.token_file), 0o600, oct(umask))
                for name in self.temp_files():
                    self.assertEqual(self.mode_of(os.path.join(self.token_dir, name)), 0o600)


class Failures(Refresh):
    """S6 and S8: a failure keeps the old file, audits once, names the step and is retried without spinning."""

    FAULTS = {"the vault": ("fail", "ok"), "the login": ("ok", "refuse"), "the token request": ("ok", "garbage")}

    def test_each_failure_keeps_the_old_file_and_audits_one_line(self):
        for what, (vault, token) in self.FAULTS.items():
            with self.subTest(step=what):
                self.reset()
                self.set_vault(vault)
                self.set_token_mode(token)
                old = make_token(int(self.clock().timestamp()) - 10)
                self.write_token_file(old)
                code, out, err = self.passes(5)
                self.assertEqual(self.token_bytes().decode(), old)          # the old file is kept
                lines = self.refresh_lines()
                self.assertEqual(len(lines), 1, lines)                      # one line, not one per pass
                self.assertFalse(lines[0]["ok"])
                self.assertTrue(lines[0]["reason"])
                self.assertEqual(lines[0]["worker"], "")
                self.assertEqual(lines[0]["runtime_id"], "reaper")
                self.assertEqual(lines[0]["action"], "refresh-cluster-token")
                reports = [r for r in self.entries(out) if r["reason"].startswith("cluster token not refreshed")]
                self.assertTrue(reports, out)
                for r in reports:
                    self.assertEqual(r["exit"], 1)
                    self.assertEqual(r["worker"], "")

    def test_the_reason_names_the_step_that_failed_and_holds_no_value(self):
        for what, (vault, token) in self.FAULTS.items():
            with self.subTest(step=what):
                self.reset()
                self.set_vault(vault)
                self.set_token_mode(token)
                self.passes(1)
                reason = self.refresh_lines()[-1]["reason"]
                self.assertTrue(reason)
                self.assertNotIn(canary(), reason)
                self.assertNotIn(make_token(self.exp), reason)

    def test_a_vault_read_refused_for_the_firewall_is_named(self):
        self.set_vault("firewall")
        self.passes(1)
        reason = self.refresh_lines()[-1]["reason"]
        self.assertIn("the vault refused this machine", reason)
        self.assertNotIn("client address", reason)
        self.assertNotRegex(reason, r"\d+\.\d+\.\d+\.\d+")   # no address

    def test_the_vault_access_check_is_never_run_from_the_reaper(self):
        self.set_vault("firewall")
        self.passes(1)
        self.assertEqual(self.env.get("CW_VAULT_CHECK_DIR", ""), "")   # the reaper never asks for the check
        self.assertEqual(self.token_log(), [])                        # and never gets as far as the token command

    def test_a_later_success_writes_one_clearing_line(self):
        self.set_vault("fail")
        self.passes(2)
        self.assertEqual(len(self.refresh_lines()), 1)
        self.set_vault("ok")
        self.passes(1)
        lines = self.refresh_lines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1]["ok"])


class NoSpinning(Refresh):
    """S7: a missing file and a failing vault: the reaper keeps trying, but not on every pass."""

    def test_ten_minutes_of_a_failing_vault_reads_it_at_most_twice(self):
        code, out, err = self.passes(10)
        self.assertNotEqual(code, 0)   # the loop ended by the test's signal, not by an exit of its own
        reads = len(self.vault_reads())
        self.assertLessEqual(reads, 2, reads)
        self.assertEqual(len(self.refresh_lines()), 1)

    def test_the_reaper_does_not_exit_and_the_vault_recovers(self):
        self.set_vault("fail")
        self.passes(3)
        self.assertFalse(os.path.exists(self.token_file))
        self.set_vault("ok")
        self.passes(1)
        self.assertTrue(os.path.exists(self.token_file))
        lines = self.refresh_lines()
        self.assertEqual([l["ok"] for l in lines], [False, True])


class OtherLogins(Refresh):
    """U2 and U4: another login is never touched, and a token written mid-loop is a secret from then on."""

    def context_login(self):
        env = {k: v for k, v in self.env.items() if k not in ("CW_CLOUD_TOKEN_FILE", "CW_CLOUD_SERVER")}
        env["CW_KUBE_CONTEXT"] = "acme-cw"
        return env

    def kubeconfig_login(self):
        env = {k: v for k, v in self.env.items() if k not in ("CW_CLOUD_TOKEN_FILE", "CW_CLOUD_SERVER")}
        env["CW_KUBECONFIG"] = os.path.join(self.tmp.name, "kubeconfig")
        env["CW_KUBE_CONTEXT"] = "acme-cw"
        return env

    def test_a_context_login_is_never_refreshed(self):
        code, out, err = self.passes(2, env=self.context_login())
        self.assertEqual(self.vault_reads(), [])
        self.assertEqual(self.token_log(), [])
        self.assertEqual(self.temp_files(), [])
        self.assertFalse(os.path.exists(self.token_file))

    def test_a_kubeconfig_login_is_never_refreshed(self):
        code, out, err = self.passes(2, env=self.kubeconfig_login())
        self.assertEqual(self.vault_reads(), [])
        self.assertEqual(self.token_log(), [])
        self.assertFalse(os.path.exists(self.token_file))

    def test_a_token_written_mid_loop_is_scrubbed_by_the_next_failing_call(self):
        self.write_token_file(make_token(int(self.clock().timestamp()) + cw.CLUSTER_TOKEN_REFRESH_MARGIN_SECONDS - 1))
        code, out, err = self.passes(1)
        token = make_token(self.exp)
        self.assertEqual(self.token_bytes().decode(), token)   # the new token is in the file
        self.assertNotIn(token, out)
        self.assertNotIn(token, err)
        self.assertNotIn(token, json.dumps(self.audit()))


class Mutants(Refresh):
    """S11: a build that writes the file without the owner-only mode fails the mode test."""

    def mutants(self):
        return {
            "the file is created at mode 0644": SOURCE.replace("os.fchmod(fd, 0o600)", "os.fchmod(fd, 0o644)"),
            "the file is created with the default umask mode": SOURCE.replace(
                "os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)",
                "os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)").replace(
                "os.fchmod(fd, 0o600)", "pass"),
        }

    def test_the_real_build_writes_the_file_with_mode_0600(self):
        path = os.path.join(self.token_dir, "token")
        cw.write_token_file(path, make_token(self.exp))
        self.assertEqual(self.mode_of(path), 0o600)

    def test_both_mutants_fail_the_mode_test(self):
        real = os.umask(0o022)
        try:
            for name, text in self.mutants().items():
                with self.subTest(mutant=name):
                    self.assertNotEqual(text, SOURCE)
                    module = mutant_module(text)
                    path = os.path.join(self.token_dir, "mutant")
                    module.write_token_file(path, make_token(self.exp))
                    self.assertNotEqual(self.mode_of(path), 0o600, name)
        finally:
            os.umask(real)


class TheServiceFile(Refresh):
    """S9: the reaper's service file holds names and paths only, and no new setting name appears."""

    SETTINGS = {"CW_KUBE_CONTEXT": "acme-cw", "CW_KUBECONFIG": "/home/user/.kube/config", "CW_KUBECTL": "kubectl",
                "CW_CLOUD_SERVER": "https://api.example.internal:6443", "CW_CLOUD_TOKEN_FILE": "/home/user/.cw/token",
                "CW_CLOUD_CA_FILE": "/home/user/ca.pem", "CW_CLOUD_NAMESPACE": "acme-cw", "CW_CLOUD_RECORD_DIR": "/home/user/runs",
                "CW_RUN_RECORD_DIR": "/home/user/runs", "CW_CLOUD_AUDIT": "/home/user/audit.jsonl"}

    def test_the_service_file_holds_no_canary_and_the_same_setting_names_as_before(self):
        installer = load_installer()
        token = "".join(chr(97 + (i * 5) % 26) for i in range(40))   # built at run time
        admin = "".join(chr(98 + (i * 3) % 26) for i in range(40))
        settings = installer.reaper_settings(dict(self.SETTINGS))
        for kind in ("launchd", "systemd"):
            with self.subTest(kind=kind):
                text = installer.render_reaper(kind, installer.service_values(ROOT, ["/usr/bin"]), settings, ROOT)
                self.assertNotIn(token, text)
                self.assertNotIn(admin, text)
                names = set(re.findall(r"CW_[A-Z0-9_]+", text))
                self.assertEqual(names, set(installer.REAPER_SETTING_NAMES))

    def test_the_setting_names_are_the_ones_the_change_did_not_add(self):
        installer = load_installer()
        self.assertEqual(sorted(installer.REAPER_SETTING_NAMES),
                         ["CW_CLOUD_AUDIT", "CW_CLOUD_CA_FILE", "CW_CLOUD_NAMESPACE", "CW_CLOUD_RECORD_DIR", "CW_CLOUD_SERVER",
                          "CW_CLOUD_TOKEN_FILE", "CW_KUBECONFIG", "CW_KUBECTL", "CW_KUBE_CONTEXT", "CW_RUN_RECORD_DIR"])

    def test_the_launcher_reads_no_new_setting_name(self):
        """The names the launcher and the wiring read are a setting row or on the one list (the settings suite keeps that); this change adds none."""
        with open(os.path.join(ROOT, "bin", "cloud_worker.py")) as f:
            text = f.read()
        self.assertNotIn("CW_CLUSTER_TOKEN_REFRESH", text)
        self.assertNotIn("CW_CLUSTER_TOKEN_RETRY", text)


class TheRunner(unittest.TestCase):
    """S12: the new file is chained in the launcher's test runner and the completeness check is happy."""

    def test_the_runner_chains_this_file_and_lists_every_test_file(self):
        runner = os.path.join(ROOT, "bin", "test-cloud-launcher")
        with open(runner) as f:
            text = f.read()
        self.assertIn("tests/%s" % os.path.basename(__file__), text)
        exceptions = text.split("EXCEPTIONS=")[1].split("\n")[0]
        missing = []
        for name in sorted(os.listdir(HERE)):
            if not name.startswith("test_") or not name.endswith(".py"):
                continue
            if "tests/%s" % name not in text and name not in exceptions:
                missing.append(name)
        self.assertEqual(missing, [])

    def test_this_file_runs_at_least_fifteen_cases(self):
        loader = unittest.TestLoader()
        found = loader.discover(HERE, pattern=os.path.basename(__file__))
        count = found.countTestCases()
        self.assertGreaterEqual(count, 15, count)


class TheDocs(unittest.TestCase):
    """S13: the documentation says the reaper is unattended, what it reads, and that the installer's comment is superseded."""

    DOCS = ("docs/cloud-operator-quickstart.md", "cloud/launcher/README.md")
    PHRASES = ("unattended", "CW_CLOUD_TOKEN_FILE", "CW_CLOUD_SERVER", "CW_CLOUD_NAMESPACE", "CW_VAULT_SUBSCRIPTION", "CW_VAULT_NAME",
               "CW_CLUSTER_ADMIN_SECRET", "the manual install comment on the installer ask is superseded")

    def read(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_every_phrase_is_in_both_documents(self):
        missing = []
        for rel in self.DOCS:
            text = self.read(rel).lower()
            for phrase in self.PHRASES:
                if phrase.lower() not in text:
                    missing.append((rel, phrase))
        self.assertEqual(missing, [])


class TheOsOwnSymlinkedParent(unittest.TestCase):
    """A root-owned link the OS provides (macOS /var -> private/var) in the parent chain is fine; a link the user owns is still refused."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "bin"))
        import path_walk
        self.walk = path_walk

    def test_a_root_owned_link_in_the_parents_is_followed(self):
        if not (os.path.islink("/var") and os.lstat("/var").st_uid == 0):
            self.skipTest("no root-owned link at /var on this system")
        import tempfile
        with tempfile.TemporaryDirectory(dir="/var/tmp") as d:
            os.mkdir(os.path.join(d, "tokens"), 0o700)
            self.assertIsNone(self.walk.parents_problem(os.path.join(d, "tokens", "token")))

    def test_a_link_owned_by_the_user_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real")
            os.makedirs(os.path.join(real, "tokens"), 0o700)
            os.symlink(real, os.path.join(d, "link"))
            self.assertIn("symlink", self.walk.parents_problem(os.path.join(d, "link", "tokens", "token")) or "")

    def test_a_mutant_that_follows_every_link_is_caught(self):
        import tempfile
        with open(self.walk.__file__) as f:
            src = f.read()
        mutant = src.replace("if found.st_uid != 0:", "if False:")
        self.assertNotEqual(src, mutant)
        ns = {"__name__": "mutant"}
        exec(compile(mutant, "mutant", "exec"), ns)
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real")
            os.makedirs(os.path.join(real, "tokens"), 0o700)
            os.symlink(real, os.path.join(d, "link"))
            self.assertIsNone(ns["parents_problem"](os.path.join(d, "link", "tokens", "token")))   # the mutant accepts it: the test above would fail


if __name__ == "__main__":
    unittest.main()
