#!/usr/bin/env python3
"""Ask #2418: the proof gate of bin/cloud_image_roll.py, the receipt writer and proof pod of bin/cloud_image_proof.py, and the Dockerfile label.
Offline: fake crane, git, cloud-worker and kubectl scripts in a scratch directory; mutants are in-memory copies of the source."""
import contextlib
import datetime
import io
import os
import re
import shutil
import stat
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ROLL = (ROOT / "bin" / "cloud_image_roll.py").read_text()
PROOF = (ROOT / "bin" / "cloud_image_proof.py").read_text()
HEX = "a" * 64
OTHER = "b" * 64
REV = "1" * 40
SRC = "src.example/team/cw-worker@sha256:" + HEX
ANCHOR = "an image was rolled without a passing proof"
NOW = datetime.datetime(2026, 11, 11, 12, 0, 0, tzinfo=datetime.timezone.utc)
ITEMS = ("pg-env", "hook", "python311", "run-wake", "tests", "identity")
CRANE = """#!/bin/sh
echo "$@" >> "$FAKE_DIR/crane.log"
case "$1" in
  config) printf '{"config":{"Labels":{"org.opencontainers.image.revision":"%s"}}}' "$(cat "$FAKE_DIR/label")"; exit 0;;
  copy) exit 0;;
  digest) printf 'sha256:%s' "$(cat "$FAKE_DIR/digest")"; exit 0;;
esac
exit 9
"""
GIT = '#!/bin/sh\necho "$@" >> "$FAKE_DIR/git.log"\nexit "$(cat "$FAKE_DIR/gitexit")"\n'
LAUNCHER = """#!/bin/sh
echo "$@" >> "$FAKE_DIR/launcher.log"
[ "$1" = create ] && echo '{"pod": "proof-pod-1"}'
exit "$(cat "$FAKE_DIR/launchexit")"
"""
KUBECTL = """#!/bin/sh
echo "$@" >> "$FAKE_DIR/kubectl.log"
case "$*" in *proof-results*) cat "$FAKE_DIR/results"; exit 0;; esac
cat > /dev/null 2>&1 || true
exit 0
"""


def load(source, name="mod"):
    m = types.ModuleType(name)
    exec(compile(source, name + ".py", "exec"), m.__dict__)
    return m


def mutant(old, new):
    assert ROLL.count(old) == 1, old
    out = ROLL.replace(old, new)
    assert out != ROLL
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="image-gate-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = os.path.join(self.tmp, "fake")
        os.mkdir(self.fake)
        for name, body in (("crane", CRANE), ("git", GIT), ("launcher", LAUNCHER), ("kubectl", KUBECTL)):
            Path(self.fake, name).write_text(body)
            os.chmod(os.path.join(self.fake, name), 0o755)
        for name, text in (("label", REV), ("digest", HEX), ("gitexit", "0"), ("launchexit", "0"), ("results", "".join("%s pass\n" % i for i in ITEMS))):
            Path(self.fake, name).write_text(text)
        self.state = os.path.join(self.tmp, "state")
        self.rdir = os.path.join(self.state, "fl" + "eet-switchboard", "cloud", "image-proof")
        os.makedirs(self.rdir)
        self.conf = os.path.join(self.tmp, "cfg")
        os.mkdir(self.conf, 0o700)
        self.settings = os.path.join(self.conf, "cloud.env")
        Path(self.settings).write_text("A=1\nCW_CLOUD_IMAGE=reg.example/cw@sha256:%s\nB=2\n" % ("c" * 64))
        os.chmod(self.settings, 0o600)
        os.utime(self.settings, (1_000_000_000, 1_000_000_000))
        self.env = {"CRANE": os.path.join(self.fake, "crane"), "GIT": os.path.join(self.fake, "git"), "FAKE_DIR": self.fake,
                    "XDG_STATE_HOME": self.state, "PATH": os.environ.get("PATH", ""), "CW_CLOUD_LAUNCHER": os.path.join(self.fake, "launcher"),
                    "CW_KUBECTL": os.path.join(self.fake, "kubectl")}

    def receipt(self, digest=HEX, verdict="pass", age_hours=1, revision=REV, name="r.receipt"):
        when = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        Path(self.rdir, name).write_text("digest: sha256:%s\ntime: %s\nrevision: %s\nverdict: %s\n" % (digest, when, revision, verdict))

    def snap(self):
        st = os.stat(self.settings)
        return Path(self.settings).read_bytes(), st.st_mtime_ns

    def log(self, name):
        p = Path(self.fake, name + ".log")
        return p.read_text().splitlines() if p.exists() else []

    def roll(self, module, extra=()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module.main([SRC, "dest.example/team/cw-worker", "v1", "--settings", self.settings] + list(extra), dict(self.env))
        return code, out.getvalue(), err.getvalue()


def s5_case(case, module, setup):
    """One refused candidate: no copy, settings unchanged. The mutant's text names the failure."""
    setup()
    before = case.snap()
    code, out, err = case.roll(module)
    if code == 0 or [l for l in case.log("crane") if l.startswith("copy ")] or case.snap() != before:
        raise AssertionError(ANCHOR)
    case.assertEqual(len(err.strip().splitlines()), 1, err)


class Gate(Base):
    def cases(self):
        def empty_label():
            Path(self.fake, "label").write_text("")
            self.receipt()
        def not_reachable():
            Path(self.fake, "gitexit").write_text("1")
            self.receipt()
        def no_receipt():
            pass
        def fail_receipt():
            self.receipt(verdict="fail")
        def other_digest():
            self.receipt(digest=OTHER)
        def old_receipt():
            self.receipt(age_hours=7)
        return [empty_label, not_reachable, no_receipt, fail_receipt, other_digest, old_receipt]

    def test_s5_six_candidates_are_refused_before_any_copy(self):
        module = load(ROLL)
        for setup in self.cases():
            self.setUp()
            s5_case(self, module, setup)
        self.assertEqual(len(self.cases()), 6)

    def test_s5_the_revision_is_asked_of_git_against_the_integration_tip(self):
        self.receipt()
        self.assertEqual(self.roll(load(ROLL))[0], 0)
        self.assertTrue(any("merge-base --is-ancestor %s origin/integration" % REV in l for l in self.log("git")), self.log("git"))

    def test_s6_a_reachable_candidate_with_a_fresh_pass_receipt_proceeds_and_no_gate_is_unknown(self):
        self.receipt()
        code, out, err = self.roll(load(ROLL))
        self.assertEqual((code, err), (0, ""), err)
        self.assertIn("copy ", "\n".join(self.log("crane")))
        self.assertIn("CW_CLOUD_IMAGE=dest.example/team/cw-worker@sha256:" + HEX, Path(self.settings).read_text())
        self.setUp()
        self.receipt(verdict="fail")
        before = self.snap()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
            self.roll(load(ROLL), ("--no-gate",))
        self.assertEqual(stop.exception.code, 2)
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.log("crane"), [])

    def test_s7_control_passes_and_a_mutant_ignoring_the_verdict_fails_with_the_anchored_text(self):
        s5_case(self, load(ROLL), lambda: self.receipt(verdict="fail"))   # the control first
        self.setUp()
        module = load(mutant('if latest.get("verdict") != "pass":', "if False:"))
        with self.assertRaises(AssertionError) as caught:
            s5_case(self, module, lambda: self.receipt(verdict="fail"))
        self.assertIn(ANCHOR, str(caught.exception))


class Receipt(Base):
    def test_s3_every_missing_or_red_item_makes_the_verdict_fail_naming_it(self):
        proof = load(PROOF)
        good = "".join("%s pass\n" % i for i in ITEMS)
        self.assertEqual(proof.judge(good)[0], "pass")
        cases = 0
        for item in ITEMS:
            missing = "".join("%s pass\n" % i for i in ITEMS if i != item)
            red = good.replace("%s pass" % item, "%s fail" % item)
            for text in (missing, red):
                verdict, rows = proof.judge(text)
                self.assertEqual(verdict, "fail", (item, text))
                self.assertEqual([i for i, r in rows if r == "fail"], [item])
                self.assertIn("failed: " + item, proof.receipt_text("sha256:" + HEX, REV, NOW, verdict, rows))
                cases += 1
        self.assertEqual(cases, 12)

    def prove(self, **kw):
        proof = load(PROOF)
        args = types.SimpleNamespace(digest=SRC, repo="acme/widget", branch="integration", wait=0)
        return proof, proof.prove(args, dict(self.env), now=lambda: NOW, sleep=lambda s: None)

    def test_s4_the_receipt_is_0600_holds_the_fields_and_the_pod_is_retired(self):
        proof, (verdict, line) = self.prove()
        self.assertEqual(verdict, "pass")
        files = os.listdir(self.rdir)
        self.assertEqual(len(files), 1)
        path = os.path.join(self.rdir, files[0])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        text = Path(path).read_text()
        self.assertEqual(text.count("digest: sha256:" + HEX), 1)
        self.assertIn("time: 2026-11-11T12:00:00Z", text)
        self.assertIn("revision: " + REV, text)
        self.assertEqual(len(re.findall(r"^item \S+ pass$", text, re.M)), 6)
        self.assertEqual([l for l in self.log("launcher") if l.startswith("stop ")].__len__(), 1)
        create = [l for l in self.log("launcher") if l.startswith("create ")][0]
        self.assertIn("--image " + SRC, create)
        self.assertEqual(Path(self.settings).read_text().count("CW_CLOUD_IMAGE=reg.example"), 1)   # the settings file is not touched

    def test_s4_a_fail_verdict_still_retires_the_pod_and_a_gate_reads_it_as_not_pass(self):
        Path(self.fake, "results").write_text("pg-env pass\n")
        proof, (verdict, line) = self.prove()
        self.assertEqual(verdict, "fail")
        self.assertEqual(len([l for l in self.log("launcher") if l.startswith("stop ")]), 1)
        self.assertIn("failed: hook", line)
        Path(self.fake, "gitexit").write_text("0")
        # the receipt just written is the one the gate reads: a fail receipt for this digest is refused (it is dated 2026, so it is also old)
        code, out, err = self.roll(load(ROLL))
        self.assertNotEqual(code, 0)

    def test_a_launch_that_fails_writes_no_receipt_and_launches_no_stop(self):
        Path(self.fake, "launchexit").write_text("1")
        proof = load(PROOF)
        args = types.SimpleNamespace(digest=SRC, repo="acme/widget", branch="integration", wait=0)
        with self.assertRaises(proof.Refused):
            proof.prove(args, dict(self.env), now=lambda: NOW, sleep=lambda s: None)
        self.assertEqual(os.listdir(self.rdir), [])


class Pins(unittest.TestCase):
    def test_s2_the_dockerfile_has_the_argument_and_the_label_and_the_readme_the_rule(self):
        text = (ROOT / "cloud" / "worker" / "Dockerfile").read_text()
        self.assertEqual(len(re.findall(r"^ARG CW_IMAGE_REVISION$", text, re.M)), 1)
        self.assertEqual(len(re.findall(r'^LABEL org\.opencontainers\.image\.revision="\$\{CW_IMAGE_REVISION\}"$', text, re.M)), 1)
        self.assertIn("Images are built only from integration's tip after merging", (ROOT / "cloud" / "README.md").read_text())

    def test_the_proof_set_names_the_six_items_and_the_eight_files(self):
        text = (ROOT / "cloud" / "worker" / "proof-set.md").read_text()
        for item in ITEMS:
            self.assertIn("`%s`" % item, text)
        self.assertEqual(len(re.findall(r"^- tests/\S+\.test\.ts$", text, re.M)), 8)


if __name__ == "__main__":
    unittest.main()
