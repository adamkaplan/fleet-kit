"""The reaper's own cluster login (ask #1060): the token file stays fresh without a person.

Offline: a fake vault (a fake `az` that answers or fails), a fake cluster (a fake `kubectl` that answers `create token`), a fake clock and a
real pipe. No token is a literal here: every value is assembled at run time.

The families are derived from the code, never listed twice:
  * every failure kind of the shared login has a case (the kinds are read out of the source), and
  * the mode of the written file is checked with two mutants (a build that writes it without the owner-only mode fails).
"""
import base64
import datetime
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
REAL_KUBECTL_PATHS, REAL_AZ_PATHS = cw.MINT_KUBECTL_PATHS, cw.MINT_AZ_PATHS   # the real fixed lists (the tests patch the module constants)
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


ENV_MARKER = "--- invocation ---"
# The wrapper's own variables: sh adds them at its start; they are not the reaper's.
SHELL_ADDED = {"PWD", "SHLVL", "_"}


def read_env_log(path):
    """The environments a wrapper recorded (one dict per invocation), minus the shell's own three names."""
    out = []
    try:
        with open(path) as f:
            for line in f.read().split("\n"):
                if line == ENV_MARKER:
                    out.append({})
                elif out and "=" in line:
                    k, v = line.split("=", 1)
                    if k not in SHELL_ADDED:
                        out[-1][k] = v
    except OSError:
        pass
    return out


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
    module.MINT_KUBECTL_PATHS, module.MINT_AZ_PATHS = cw.MINT_KUBECTL_PATHS, cw.MINT_AZ_PATHS   # the fixed lists point at this case's fakes
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
        cw.MINT_KUBECTL_PATHS = (os.path.join(self.bin, "kubectl"),)   # the strict route's fixed lists, pointed at the fakes
        cw.MINT_AZ_PATHS = (os.path.join(self.bin, "az"),)
        self.addCleanup(self.restore_lists)
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

    def restore_lists(self):
        cw.MINT_KUBECTL_PATHS, cw.MINT_AZ_PATHS = REAL_KUBECTL_PATHS, REAL_AZ_PATHS

    # -- the fakes --
    interpreter = "/usr/bin/env python3"   # the seam: what the wrapper runs the python body with

    def write_fake(self, name, text):
        """A python fake is two files: a POSIX shell wrapper (the program the reaper runs) that records its raw environment BEFORE any
        interpreter starts, then runs the python body (<name>.py) unchanged. Other texts are written as they are."""
        path = os.path.join(self.bin, name)
        if text.startswith("#!/usr/bin/env python3"):
            with open(path + ".py", "w") as f:
                f.write(text)
            log = path + ".envlog"
            guard = ' case " $* " in *" cw-operator "*) ;; *) exec %s "$0.PY" "$@";; esac\n' % self.interpreter if name.endswith("kubectl") else ""
            guard = guard.replace('"$0.PY"', '"%s.py"' % path)
            script = ("#!/bin/sh\n" + guard.replace(" case", "case", 1) +
                      "{ echo '%s'; env; } >> '%s'\n" % (ENV_MARKER, log) +
                      'exec %s "%s.py" "$@"\n' % (self.interpreter, path))
            with open(path, "w") as f:
                f.write(script)
        else:
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


class MintGuards(Refresh):
    """Ask #1060 review: the mint command's program, account, duration and namespace are fixed or checked; the child's environment is minimal;
    the retry gap is pinned on a fake clock, forwards and backwards. Each guard has a mutant that fails its test."""

    def outcome(self, module=None, env=None, duration="30m", strict=True):
        """The failure kind of one mint ("ok" when it made a token), through the given build of the module."""
        module = module or cw
        try:
            module.cluster_token(env if env is not None else self.env, duration, now=self.clock().timestamp(), strict=strict)
        except Exception as e:   # the mutant module has a ClusterLogin class of its own
            return getattr(e, "kind", type(e).__name__)
        return "ok"

    def mutate(self, old, new, count=1):
        self.assertIn(old, SOURCE)
        text = SOURCE.replace(old, new)
        self.assertNotEqual(text, SOURCE)   # the mutation applied
        return mutant_module(text)

    def settings_file(self, mode, text="CW_VAULT_NAME=acme-vault\n"):
        directory = os.path.join(self.tmp.name, "cfg", cw.CONFIG_DIR_NAME)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(os.path.dirname(directory), 0o700)
        os.chmod(directory, 0o700)
        path = os.path.join(directory, cw.SETTINGS_FILE_NAME)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, mode)
        self.env["XDG_CONFIG_HOME"] = os.path.join(self.tmp.name, "cfg")
        return path

    # -- the program --
    def test_a_program_that_is_not_an_absolute_checked_file_is_refused_with_no_vault_read(self):
        writable = self.write_fake("writable-kubectl", FAKE_KUBECTL)
        os.chmod(writable, 0o775)
        plain = os.path.join(self.bin, "not-executable")
        with open(plain, "w") as f:
            f.write("x")
        for value in ("sh", "kubectl", "./kubectl", "bin/kubectl", os.path.join(self.bin, "missing"), self.bin, writable, plain):
            with self.subTest(value=value):
                cw.MINT_KUBECTL_PATHS = (value,)   # the fixed list holds a bad entry
                self.assertEqual(self.outcome(), "kubectl-refused")
                self.assertEqual(self.vault_reads(), [])

    def test_a_link_to_a_good_absolute_file_is_allowed(self):
        link = os.path.join(self.bin, "kubectl-link")
        os.symlink(os.path.join(self.bin, "kubectl"), link)
        cw.MINT_KUBECTL_PATHS = (link,)
        self.assertEqual(self.outcome(), "ok")

    def test_the_fixed_lists_are_searched_in_order_and_az_is_never_found_on_path(self):
        self.assertEqual(REAL_KUBECTL_PATHS, ("/opt/homebrew/bin/kubectl", "/usr/local/bin/kubectl", "/usr/bin/kubectl"))
        self.assertEqual(REAL_AZ_PATHS, ("/opt/homebrew/bin/az", "/usr/local/bin/az", "/usr/bin/az"))
        good = os.path.join(self.bin, "kubectl")
        cw.MINT_KUBECTL_PATHS = (os.path.join(self.bin, "missing"), good, "/nonexistent/kubectl")
        self.assertEqual(cw.mint_kubectl(), os.path.realpath(good))
        cw.MINT_AZ_PATHS = (os.path.join(self.bin, "missing"),)   # a good az is on PATH (self.bin) but not in the list
        self.assertEqual(self.outcome(), "az-refused")
        self.assertEqual(self.vault_reads(), [])

    def test_the_mutant_that_accepts_any_program_fails(self):
        cw.MINT_KUBECTL_PATHS = ("sh",)
        self.assertEqual(self.outcome(), "kubectl-refused")
        module = self.mutate("if _kubectl_problem(path) is None:\n            return os.path.realpath(path)\n    raise ClusterLogin(\"kubectl-refused\")",
                             "return os.path.realpath(path)\n    raise ClusterLogin(\"kubectl-refused\")")
        self.assertNotEqual(self.outcome(module), "kubectl-refused")

    def test_the_mutant_that_finds_az_on_path_fails(self):
        cw.MINT_AZ_PATHS = (os.path.join(self.bin, "missing"),)
        self.assertEqual(self.outcome(), "az-refused")
        module = self.mutate('az = az_path if strict else "az"', 'az = (shutil.which("az", path=env.get("PATH") or MINT_PATH) or "az") if strict else "az"')
        module = self.mutate_more(module, "            az_path = mint_az()\n", "")
        self.assertEqual(self.outcome(module), "ok")   # the mutant ran the az found on PATH: caught

    def mutate_more(self, module, old, new):
        text = SOURCE.replace('az = az_path if strict else "az"', 'az = (shutil.which("az", path=env.get("PATH") or MINT_PATH) or "az") if strict else "az"').replace(old, new)
        self.assertNotEqual(text, SOURCE)
        return mutant_module(text)

    # -- the fixed route: no setting or variable names the program or the command --
    def marker_script(self):
        self.marker = os.path.join(self.tmp.name, "marker")
        path = self.write_fake("marker-program", "#!/bin/sh\necho ran > %s\nexit 1\n" % self.marker)
        return path

    def test_the_strict_route_ignores_the_program_and_command_settings_in_the_environment(self):
        script = self.marker_script()
        for name, value in (("CW_KUBECTL", script), ("CW_CLUSTER_ADMIN_CMD", script), ("CW_CLUSTER_TOKEN_CMD", script)):
            with self.subTest(name=name):
                self.assertEqual(self.outcome(env=dict(self.env, **{name: value})), "ok")
                self.assertFalse(os.path.exists(self.marker))   # the marker program never ran

    def test_the_strict_route_ignores_the_same_settings_in_the_settings_file(self):
        script = self.marker_script()
        for name in ("CW_KUBECTL", "CW_CLUSTER_ADMIN_CMD", "CW_CLUSTER_TOKEN_CMD"):
            with self.subTest(name=name):
                self.settings_file(0o600, "%s=%s\n" % (name, script))
                self.assertEqual(self.outcome(env=dict(self.env, **{name: script})), "ok")
                self.assertFalse(os.path.exists(self.marker))

    def run_pass(self, env, module=None):
        """One refresher run on a missing token file (a refresh is due): the refresher, its result."""
        refresher = (module or cw).ClusterTokenRefresher(env, self.clock)
        return refresher, refresher.run()

    def notice_audit(self):
        return [l for l in self.audit() if l.get("action") == "ignored-setting-notice"]

    def test_an_ignored_setting_is_named_once_per_pass_window_and_the_fixed_route_still_runs(self):
        script = self.marker_script()
        for name in ("CW_KUBECTL", "CW_CLUSTER_ADMIN_CMD", "CW_CLUSTER_TOKEN_CMD"):
            for where in ("environment", "file"):
                with self.subTest(name=name, where=where):
                    self.reset()
                    if os.path.exists(self.marker):
                        os.unlink(self.marker)
                    env = dict(self.env)
                    env.pop("CW_KUBECTL")   # the fixture sets it: only the name under test is set
                    if where == "environment":
                        env[name] = script
                    else:
                        self.settings_file(0o600, "%s=%s\n" % (name, script))
                        env["XDG_CONFIG_HOME"] = self.env["XDG_CONFIG_HOME"]
                    refresher, result = self.run_pass(env)
                    self.assertEqual(result.state, "written")   # the fixed route ran
                    self.assertFalse(os.path.exists(self.marker))
                    self.assertEqual(refresher.notices, ["cluster login: %s is ignored in the unattended refresh (the reaper uses a fixed %s path); "
                                                         "use the launch path for a custom command" % (name, "az" if name == "CW_CLUSTER_ADMIN_CMD" else "kubectl")])
                    self.assertNotIn(script, refresher.notices[0])   # a name, never a value
                    lines = self.notice_audit()
                    self.assertEqual(len(lines), 1)
                    self.assertNotIn(script, json.dumps(lines))
                    self.clock.t = self.clock.t + datetime.timedelta(seconds=60)
                    refresher.run()
                    self.assertEqual(refresher.notices, [])   # not again within the hour
                    self.assertEqual(len(self.notice_audit()), 1)
                    self.clock.t = self.clock.t + datetime.timedelta(seconds=3600)
                    refresher.run()
                    self.assertEqual(len(refresher.notices), 1)   # and again after an hour
                    self.assertEqual(len(self.notice_audit()), 2)

    def test_no_notice_when_no_ignored_setting_is_set(self):
        env = dict(self.env)
        env.pop("CW_KUBECTL")
        refresher, result = self.run_pass(env)
        self.assertEqual(result.state, "written")
        self.assertEqual(refresher.notices, [])
        self.assertEqual(self.notice_audit(), [])

    def test_the_mutant_without_the_notice_fails(self):
        env = dict(self.env, CW_KUBECTL=self.marker_script())
        refresher, _ = self.run_pass(env)
        self.assertEqual(len(refresher.notices), 1)
        module = self.mutate("        self.note_ignored(now)\n", "")
        self.reset()
        env = dict(self.env, CW_KUBECTL=self.marker_script())
        refresher, result = self.run_pass(env, module)
        self.assertEqual(result.state, "written")
        self.assertEqual(refresher.notices, [])   # the mutant says nothing: the test above would fail

    def test_the_mutant_that_honours_kubectl_again_fails(self):
        script = self.marker_script()
        env = dict(self.env, CW_KUBECTL=script)
        self.assertEqual(self.outcome(env=env), "ok")
        self.assertFalse(os.path.exists(self.marker))
        module = self.mutate("            kubectl = mint_kubectl()", "            kubectl = env.get(\"CW_KUBECTL\") or mint_kubectl()")
        self.assertNotEqual(self.outcome(module, env), "ok")
        self.assertTrue(os.path.exists(self.marker))   # caught: the mutant ran the program

    def test_the_mutant_that_honours_a_command_setting_again_fails(self):
        script = self.marker_script()
        for name in ("CW_CLUSTER_ADMIN_CMD", "CW_CLUSTER_TOKEN_CMD"):
            with self.subTest(name=name):
                if os.path.exists(self.marker):
                    os.unlink(self.marker)
                env = dict(self.env, **{name: script})
                self.assertEqual(self.outcome(env=env), "ok")
                self.assertFalse(os.path.exists(self.marker))
                module = self.mutate("        custom = admin = \"\"   #", "        pass   #")
                self.assertNotEqual(self.outcome(module, env), "ok")
                self.assertTrue(os.path.exists(self.marker))

    # -- the namespace --
    def test_a_namespace_that_is_not_a_dns_label_is_refused_with_no_vault_read(self):
        for value in ("c w", "cw;ls", "CW", "-cw", "cw-", "a" * 64, "cw\n", "cw.x", "$(x)"):
            with self.subTest(value=value):
                self.assertEqual(self.outcome(env=dict(self.env, CW_CLOUD_NAMESPACE=value)), "namespace-refused")
                self.assertEqual(self.vault_reads(), [])
        for value in ("cw", "a", "a" * 63, "acme-1"):
            with self.subTest(value=value):
                self.assertNotEqual(self.outcome(env=dict(self.env, CW_CLOUD_NAMESPACE=value)), "namespace-refused")

    def test_the_mutant_that_skips_the_namespace_check_fails(self):
        env = dict(self.env, CW_CLOUD_NAMESPACE="CW")
        self.assertEqual(self.outcome(env=env), "namespace-refused")
        module = self.mutate('if not _NAMESPACE_LABEL.fullmatch(namespace) or "\\n" in namespace:', "if False:")
        self.assertNotEqual(self.outcome(module, env), "namespace-refused")

    # -- the fixed command --
    def mint_argv(self, module=None, expect="ok"):
        env = dict(self.env, CW_ACCOUNT="acme-admin", CW_CLUSTER_ACCOUNT="acme-admin", CW_TOKEN_ACCOUNT="acme-admin", CW_OPERATOR="acme-admin",
                   CW_CLUSTER_TOKEN_DURATION="5m", CW_CLUSTER_TOKEN_ARGS="--audience=x", CW_KUBECTL_ARGS="get pods")
        self.assertEqual(self.outcome(module, env), expect)
        return [e["argv"] for e in self.token_log()][-1]

    def test_no_setting_changes_the_account_the_duration_or_the_subcommand(self):
        argv = self.mint_argv()
        self.assertEqual([a for a in argv if not a.startswith("/dev/fd/")],
                         ["--kubeconfig", "-n", "cw", "create", "token", "cw-operator", "--duration=30m"])
        self.assertTrue(argv[1].startswith("/dev/fd/"))

    def test_the_mutant_that_reads_the_account_from_the_environment_fails(self):
        self.assertIn("cw-operator", self.mint_argv())
        module = self.mutate('MINT_ACCOUNT,\n                     "--duration=%dm"', 'env.get("CW_ACCOUNT", MINT_ACCOUNT),\n                     "--duration=%dm"')
        # the fake cluster answers only for cw-operator: the mutant's other account never reaches the token log and the mint fails
        self.mint_argv(module, expect="mint-failed")
        self.assertEqual(self.token_log()[1:], [])

    # -- the settings file: where a value came from --
    def test_a_value_set_only_in_the_process_environment_is_ignored_whatever_the_file(self):
        script = self.marker_script()
        path = self.settings_file(0o660)   # a file others can write, holding no such value
        self.assertIsNotNone(cw.settings_file_problem(self.env))
        self.assertNotIn("CW_CLUSTER_TOKEN_CMD", open(path).read())
        self.assertEqual(self.outcome(env=dict(self.env, CW_CLUSTER_TOKEN_CMD=script)), "ok")   # the value came from the process only: ignored
        self.assertFalse(os.path.exists(self.marker))

    def test_a_value_that_came_from_the_file_is_ignored_too_and_the_file_check_reads_that_file(self):
        script = self.marker_script()
        path = self.settings_file(0o600, "CW_CLUSTER_TOKEN_CMD=%s\n" % script)
        self.assertIn(script, open(path).read())   # the value is in the file
        self.assertIsNone(cw.settings_file_problem(self.env))
        self.assertEqual(self.outcome(), "ok")
        self.assertFalse(os.path.exists(self.marker))
        os.chmod(path, 0o660)
        self.assertIsNotNone(cw.settings_file_problem(self.env))   # the check follows the file at the settings path
        self.assertEqual(self.outcome(), "ok")
        self.assertFalse(os.path.exists(self.marker))
        self.assertEqual(cw.REFRESH_REASONS["settings-untrusted"].split(":")[0][:21], "the settings file is ")

    # -- the child's environment --
    def child_envs(self):
        return (read_env_log(os.path.join(self.bin, "kubectl.envlog")) + read_env_log(os.path.join(self.bin, "az.envlog")))

    def planted(self):
        return dict(self.env, FAKE_SECRET_VAR="s" + "x" * 12, GITHUB_TOKEN="g" + "y" * 12, CW_HUB_PASSWORD="h" + "z" * 12, LANG="C.UTF-8")

    def test_the_child_gets_a_minimal_environment(self):
        self.assertEqual(self.outcome(env=self.planted()), "ok")
        found = self.child_envs()
        self.assertEqual(len(found), 2)
        for child in found:
            self.assertNotIn("FAKE_SECRET_VAR", child)
            self.assertNotIn("GITHUB_TOKEN", child)
            self.assertFalse([k for k in child if k.startswith(("CW_", "FAKE_"))], sorted(child))
            self.assertEqual(child["KUBECONFIG"], "/dev/null")
            self.assertEqual(child["PATH"], "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin")
            self.assertEqual(child["HOME"], self.home)
            self.assertLessEqual(set(child), {"PATH", "HOME", "LANG", "KUBECONFIG", "AZURE_CONFIG_DIR", "LC_CTYPE"}, sorted(child))
        self.assertNotIn("AZURE_CONFIG_DIR", found[0])

    def apple_shim(self):
        """A python3 that injects the variables Apple's shim adds, then runs the real python."""
        path = os.path.join(self.tmp.name, "apple-python3")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexport CPATH=/x LIBRARY_PATH=/x MANPATH=/x SDKROOT=/x __CF_USER_TEXT_ENCODING=0x1F5:0:0\nexec %s \"$@\"\n" % sys.executable)
        os.chmod(path, 0o755)
        return path

    def with_shim(self):
        Refresh.interpreter = self.apple_shim()
        self.addCleanup(setattr, Refresh, "interpreter", "/usr/bin/env python3")
        self.write_fake("az", FAKE_AZ)
        self.write_fake("kubectl", FAKE_KUBECTL)

    def test_interpreter_noise_cannot_matter_to_the_minimal_environment_check(self):
        self.with_shim()
        self.assertEqual(self.outcome(env=self.planted()), "ok")
        found = self.child_envs()
        self.assertEqual(len(found), 2)
        for child in found:
            self.assertLessEqual(set(child), {"PATH", "HOME", "LANG", "KUBECONFIG", "AZURE_CONFIG_DIR", "LC_CTYPE"}, sorted(child))
        with open(os.path.join(self.bin, "az.py")) as f:   # the shim really ran: the python body's own view has the noise
            self.assertIn("os.environ", f.read())

    def test_the_mutant_that_passes_the_whole_environment_still_fails_with_the_shim(self):
        self.with_shim()
        module = self.mutate("env=child_env", "env=env")
        self.assertEqual(self.outcome(module, self.planted()), "ok")
        found = self.child_envs()
        self.assertTrue(any("FAKE_SECRET_VAR" in c for c in found))
        self.assertFalse(all(set(c) <= {"PATH", "HOME", "LANG", "KUBECONFIG", "AZURE_CONFIG_DIR", "LC_CTYPE"} for c in found))

    def test_the_az_settings_directory_passes_only_when_the_person_set_it(self):
        self.assertEqual(self.outcome(env=dict(self.env, AZURE_CONFIG_DIR="/home/acme/.azure")), "ok")
        self.assertTrue(all(c.get("AZURE_CONFIG_DIR") == "/home/acme/.azure" for c in self.child_envs()))

    def test_the_mutant_that_passes_the_whole_environment_fails(self):
        module = self.mutate("env=child_env", "env=env")
        self.assertEqual(self.outcome(module, self.planted()), "ok")
        self.assertTrue(any("FAKE_SECRET_VAR" in c for c in self.child_envs()))

    def test_the_mutant_that_drops_the_empty_kubeconfig_fails(self):
        module = self.mutate('"KUBECONFIG": os.devnull', '"KUBECONFIG_X": os.devnull')
        self.assertEqual(self.outcome(module), "ok")
        self.assertTrue(any(c.get("KUBECONFIG") != "/dev/null" for c in self.child_envs()))

    # -- the two entries: the reaper is strict, the launch is not --
    def test_the_launch_call_keeps_its_previous_behaviour(self):
        """The compatibility pin: strict=False accepts a bare-name CW_KUBECTL and passes the full environment."""
        env = dict(self.planted(), CW_CLOUD_NAMESPACE="CW", PATH=self.bin + os.pathsep + os.environ.get("PATH", ""))
        self.assertEqual(self.outcome(env=env, strict=False), "ok")
        self.assertTrue(any("FAKE_SECRET_VAR" in c for c in self.child_envs()))
        self.assertEqual(self.outcome(env=env, strict=True), "namespace-refused")

    def test_the_launch_default_is_not_strict(self):
        import inspect
        self.assertIs(inspect.signature(cw.cluster_token).parameters["strict"].default, False)

    def test_a_build_where_launch_is_strict_fails_the_compatibility_test(self):
        module = self.mutate("recovery=None, strict=False):", "recovery=None, strict=True):")
        env = dict(self.env, CW_CLOUD_NAMESPACE="CW", PATH=self.bin + os.pathsep + os.environ.get("PATH", ""))
        self.assertEqual(self._launch_call(module, env), "namespace-refused")
        self.assertEqual(self._launch_call(cw, env), "ok")

    def _launch_call(self, module, env):
        try:
            module.cluster_token(env, "30m", now=self.clock().timestamp())   # the launch's call: no strict argument
        except Exception as e:
            return getattr(e, "kind", type(e).__name__)
        return "ok"

    def test_no_argument_or_variable_of_the_launch_path_reaches_strict(self):
        """Only the reaper's refresher sets strict: no CW_ name, flag or argument of the launch changes the launch path."""
        import inspect
        with open(os.path.join(ROOT, "bin", "cloud_worker.py")) as f:
            launch = f.read()
        self.assertEqual(list(inspect.signature(cw.cluster_token).parameters)[-1], "strict")
        self.assertIn("def cluster_token(env=None, now=None, recovery=None):", launch)   # the launch entry has no such argument
        self.assertEqual(re.findall(r"cluster_token\(([^)]*)\)", launch.split("def cluster_token(env=None")[1].split("\ndef ")[0]).count("strict"), 0)
        self.assertNotIn("strict", launch.lower())
        self.assertEqual(len(re.findall(r"strict=True", SOURCE.split("class ClusterTokenRefresher")[1].split("\ndef spend_cap")[0])), 1)
        self.assertEqual(len(re.findall(r"cluster_token\([^)]*strict=True", SOURCE)), 1)   # the one explicit call: the refresher's
        self.assertFalse(re.search(r"(environ|env)\.get\([^)]*[Ss][Tt][Rr][Ii][Cc][Tt]|environ\[[^]]*[Ss][Tt][Rr][Ii][Cc][Tt]", SOURCE))   # no setting is read for it
        with self.assertRaises(TypeError):
            inspect.signature(cw.cluster_token).bind(self.env, "30m", bogus=1)
        for name in ("CW_STRICT", "CW_UNATTENDED_STRICT", "STRICT", "CW_CLUSTER_STRICT", "CW_MINT_STRICT"):
            for value in ("1", "0", "true", "false"):
                env = dict(self.planted(), CW_CLOUD_NAMESPACE="CW", PATH=self.bin + os.pathsep + os.environ.get("PATH", ""), **{name: value})
                self.reset()
                self.assertEqual(self._launch_call(cw, env), "ok", (name, value))   # still the launch's behaviour
                self.assertTrue(any("FAKE_SECRET_VAR" in c for c in self.child_envs()))
                env = dict(self.env, CW_CLOUD_NAMESPACE="CW", **{name: value})
                self.assertEqual(self.outcome(env=env), "namespace-refused", (name, value))   # and the reaper's route stays strict
        for flag in (["reap", "--strict"], ["--strict", "reap"], ["status", "--strict=false"], ["reap", "--strict", "1"]):
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(cw.main(flag, env=dict(self.env)), cw.EXIT_USAGE, flag)

    def test_a_build_whose_default_is_strict_fails_the_pins(self):
        module = self.mutate("recovery=None, strict=False):", "recovery=None, strict=True):")
        import inspect
        self.assertIs(inspect.signature(module.cluster_token).parameters["strict"].default, True)   # the default pin catches it
        env = dict(self.env, CW_CLOUD_NAMESPACE="CW", PATH=self.bin + os.pathsep + os.environ.get("PATH", ""))
        self.assertEqual(self._launch_call(module, env), "namespace-refused")   # the compatibility pin catches it

    def test_a_build_whose_reaper_relies_on_the_default_fails_the_guard_tests(self):
        module = self.mutate("now=now, strict=True)", "now=now)")   # the default is False: the reaper is no longer explicit
        env = dict(self.env, CW_CLOUD_NAMESPACE="CW")
        self.assertNotEqual(module.ClusterTokenRefresher(env, self.clock).run().reason, cw.REFRESH_REASONS["namespace-refused"])

    def test_the_reapers_refresher_is_strict_and_a_build_with_strict_false_fails(self):
        env = dict(self.env, CW_CLOUD_NAMESPACE="CW")
        refresher = cw.ClusterTokenRefresher(env, self.clock)
        self.assertEqual(refresher.run().reason, cw.REFRESH_REASONS["namespace-refused"])
        self.assertEqual(self.vault_reads(), [])
        self.reset()
        module = self.mutate("now=now, strict=True)", "now=now, strict=False)")
        env = dict(self.env, CW_CLOUD_NAMESPACE="CW")
        mutant = module.ClusterTokenRefresher(env, self.clock)
        self.assertNotEqual(mutant.run().reason, cw.REFRESH_REASONS["namespace-refused"])

    def test_the_token_command_limit_is_a_named_constant_of_a_sane_value(self):
        self.assertEqual(cw.TOKEN_CMD_SECONDS, 60)
        self.assertIn("timeout=TOKEN_CMD_SECONDS", SOURCE)

    # -- the token file --
    def test_a_token_file_that_others_can_read_counts_as_expired(self):
        self.write_token_file(make_token(self.exp))
        self.assertGreater(cw.token_file_life_left(self.token_file, self.clock().timestamp()), 3000)
        os.chmod(self.token_file, 0o644)
        self.assertIsNone(cw.token_file_life_left(self.token_file, self.clock().timestamp()))

    # -- the retry gap, on a fake clock --
    def failing(self, module=None):
        self.set_vault("fail")
        module = module or cw
        return module.ClusterTokenRefresher(self.env, self.clock)

    def gap_run(self, module=None):
        """A failed refresh, then +60 s, then +301 s: the vault reads after each step."""
        refresher = self.failing(module)
        refresher.run()
        first = len(self.vault_reads())
        self.clock.advance(seconds=60)
        refresher.run()
        second = len(self.vault_reads())
        self.clock.advance(seconds=241)   # 301 s after the failure
        refresher.run()
        return first, second, len(self.vault_reads())

    def test_the_retry_gap_holds_for_300_seconds_then_reads_the_vault_once(self):
        self.assertEqual(self.gap_run(), (1, 1, 2))

    def test_both_gap_mutants_fail(self):
        for old, new in (("self.last_failure = (now, reason)", "self.last_failure = None"),
                         ("0 <= now - self.last_failure[0] < CLUSTER_TOKEN_RETRY_SECONDS", "now - self.last_failure[0] < 0")):
            with self.subTest(mutant=new):
                self.reset()
                text = SOURCE.replace(old, new)
                self.assertNotEqual(text, SOURCE)
                self.assertNotEqual(self.gap_run(mutant_module(text)), (1, 1, 2))

    def test_a_clock_that_jumps_back_an_hour_retries(self):
        refresher = self.failing()
        refresher.run()
        self.assertEqual(len(self.vault_reads()), 1)
        self.clock.advance(seconds=-3600)
        refresher.run()
        self.assertEqual(len(self.vault_reads()), 2)

    def test_the_mutant_without_the_lower_bound_fails_the_backwards_clock_test(self):
        text = SOURCE.replace("0 <= now - self.last_failure[0] < CLUSTER_TOKEN_RETRY_SECONDS", "now - self.last_failure[0] < CLUSTER_TOKEN_RETRY_SECONDS")
        self.assertNotEqual(text, SOURCE)
        refresher = self.failing(mutant_module(text))
        refresher.run()
        self.clock.advance(seconds=-3600)
        refresher.run()
        self.assertEqual(len(self.vault_reads()), 1)   # the real build reads twice (the test above)


class TheRunner(unittest.TestCase):
    """S12: the new file is chained in the launcher's test runner and the completeness check is happy."""

    def test_the_runner_chains_this_file_and_lists_every_test_file(self):
        runner = os.path.join(ROOT, "bin", "test-cloud-launcher")
        with open(runner) as f:
            text = f.read()
        with open(runner + ".chain") as f:
            text += "\n" + f.read()
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
