"""Slice K2 (#563): the operator driver (run_task.py). Scenarios S5 (every end cleans up, no secret in a file) and the
checklist cases of a secret-handling driver. Offline: the fake kubectl (it becomes a stub headless server on port-forward), no
cluster. The planted values are synthetic, in several lengths and encodings."""
import base64
import json
import os
import re
import signal
import subprocess
import sys
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw

DRIVER = os.path.join(os.path.dirname(HERE), "run_task.py")
REPO = "acme-org/widget-service"
MODEL_VALUES = ["ab12CD34e", "modelvalue-Qx7Lm2Qx7Lm2Qx7Lm2", "m" * 7 + "-" + "Zk9Wp3" * 12]   # short, medium, long
OTHER = "othervalue-Zk9Wp3Zk9Wp3Zk9Wp3Zk9Wp3"   # another provider in the same auth file: never read out
GIT = "gitvalue-Rt4Vb8Rt4Vb8Rt4Vb8Rt4Vb8"
ENTRY = {"type": "oauth", "refresh": MODEL_VALUES[0], "access": MODEL_VALUES[1], "extra": MODEL_VALUES[2]}
BRIEF = "Add a file named hello.txt."
RUN = r"per-run secret %s-run (cleaned|REMAINS)"


def forms(v):
    return cw.secret_forms(v)


class Driver(base.Base):
    def setUp(self):
        super().setUp()
        d = self.tmp.name
        self.scratch = os.path.join(d, "scratch")
        os.mkdir(self.scratch)
        self.stub_log = os.path.join(d, "stub.log")
        self.auth = os.path.join(d, "auth.json")
        with open(self.auth, "w") as f:
            json.dump({"github-copilot": ENTRY, "other-provider": {"key": OTHER}}, f)
        self.gitcmd, self.git_ran = os.path.join(d, "git-token-cmd"), os.path.join(d, "git-ran")
        with open(self.gitcmd, "w") as f:
            f.write('#!/bin/sh\ntouch "%s"\n[ "$GH_REPO" = %s ] || exit 1\nprintf "%%s" "%s"\n' % (self.git_ran, REPO, GIT))
        os.chmod(self.gitcmd, 0o700)
        self.brief = os.path.join(d, "brief.txt")
        with open(self.brief, "w") as f:
            f.write(BRIEF)
        self.seen = os.path.join(d, "seen.jsonl")
        self.mark = os.path.join(d, "hang.mark")
        self.name = cw.pod_name("coder7")
        self.patch_state(hook_result={"pushed": True, "wip_branch": "coder7/wip"})
        self.denv = dict({"PATH": os.environ["PATH"], "HOME": d}, **self.env, TMPDIR=self.scratch, CW_TMPDIR=self.scratch, CW_MODEL_AUTH_FILE=self.auth,
                         CW_GIT_TOKEN_CMD=self.gitcmd, FAKE_SCAN_DIR=self.scratch, FAKE_KUBE_SEEN=self.seen,
                         FAKE_SCAN_VALUES=",".join(MODEL_VALUES + [GIT, OTHER, json.dumps({"github-copilot": ENTRY})]),
                         STUB_LOG=self.stub_log, CW_POLL_SECONDS="0.2", CW_HEALTH_TIMEOUT="3", CW_RUN_TIMEOUT="60", CW_CREATE_TIMEOUT="60")

    def args(self, worker="coder7", repo=REPO, brief=None):
        return [sys.executable, DRIVER, worker, repo, "main", brief or self.brief, "--pull-secret", "cw-pull", "--image", "registry.example.internal/cw-worker:1"]

    def run_driver(self, **env):
        p = subprocess.run(self.args(), env=dict(self.denv, **env), capture_output=True, text=True, timeout=120)
        return p, p.stdout + p.stderr

    def spawn(self, **env):
        return subprocess.Popen(self.args(), env=dict(self.denv, **env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def read(self, path):
        if not os.path.exists(path):
            return ""
        with open(path, errors="replace") as f:
            return f.read()

    def everything(self, outputs):
        return "\n".join([outputs, self.read(self.state + ".calls"), self.read(self.env["FAKE_KUBE_CALLS"]), self.read(self.seen),
                          self.read(self.env["CW_CLOUD_AUDIT"])])

    def assert_no_secret(self, text, what="output"):
        for v in MODEL_VALUES + [GIT, OTHER, json.dumps({"github-copilot": ENTRY})]:
            for form in forms(v):
                self.assertNotIn(form, text, what)

    def assert_clean(self, outputs, said="cleaned", kept=False):
        """The three guarantees of S5: nothing left in the cluster, nothing left or leaked on disk, one statement."""
        st = self.st()
        if kept:   # a pod kept for unpushed work keeps only its own password Secret; the per-run Secret is gone
            self.assertEqual(set(st.get("secrets", {})), {self.name + "-pw"})
            self.assertIn(self.name, st["pods"])
        else:
            self.assertEqual(st.get("secrets", {}), {}, "a Secret remains")
            self.assertEqual(st["pods"], {}, "the pod remains")
        self.assertEqual(os.listdir(self.scratch), [], "temp files left behind")
        self.assertFalse(os.path.exists(self.state + ".leaks"), "a planted value was in a temp file while the driver ran")
        self.assert_no_secret(self.everything(outputs), "outputs, argv, environments of children, audit")
        self.assertEqual(len(re.findall(RUN % re.escape(self.name), outputs)), 1, outputs)
        self.assertIn("per-run secret %s-run %s" % (self.name, said), outputs)

    # -- S5: the three ends --
    def test_s5_a_normal_end_runs_the_task_stops_the_worker_and_cleans_up(self):
        p, out = self.run_driver()
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("prompt " + BRIEF, self.read(self.stub_log))
        self.assertIn("stopped; result", out)
        self.assert_clean(out)
        sent = self.read(self.state + ".stdin")   # the values did reach kubectl, by stdin; the other provider's did not
        self.assertIn(base64.b64encode(json.dumps({"github-copilot": ENTRY}).encode()).decode(), sent)
        self.assertIn(GIT, sent)
        self.assertNotIn(OTHER, sent)
        self.assertNotIn(base64.b64encode(OTHER.encode()).decode(), sent)

    def test_s5_a_failure_midway_stops_the_worker_and_cleans_up(self):
        p, out = self.run_driver(STUB_MODE="never-healthy", CW_HEALTH_TIMEOUT="1")
        self.assertEqual(p.returncode, cw.EXIT_CLOUD, out)
        self.assertIn("did not become healthy", out)
        self.assert_clean(out)

    def interrupt(self, sig, ready, **env):
        proc = self.spawn(**env)
        end = time.time() + 40
        while time.time() < end and not ready():
            time.sleep(0.1)
        self.assertTrue(ready(), "the driver never reached the phase")
        time.sleep(0.5)
        proc.send_signal(sig)
        out, err = proc.communicate(timeout=120)
        return proc.returncode, out + err

    def test_s5_an_interrupt_between_secret_creation_and_pod_creation_cleans_up(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(sig=sig):
                self.setUp()
                rc, out = self.interrupt(sig, lambda: os.path.exists(self.mark), FAKE_KUBE_HANG="label", FAKE_KUBE_HANG_MARK=self.mark)
                self.assertEqual(rc, 130, out)
                self.assertEqual(self.st().get("secrets", {}), {})
                self.assertEqual(self.st()["pods"], {})
                self.assertEqual(os.listdir(self.scratch), [])
                self.assertEqual(len(re.findall(RUN % re.escape(self.name), out)), 1, out)
                self.assertIn("cleaned", out)
                self.assert_no_secret(self.everything(out))

    def test_s5_an_interrupt_between_pod_start_and_the_prompt_cleans_up(self):
        rc, out = self.interrupt(signal.SIGINT, lambda: bool(self.st()["pods"]) and self.pf_up(), STUB_MODE="never-healthy", CW_HEALTH_TIMEOUT="60")
        self.assertEqual(rc, 130, out)
        self.assertNotIn("prompt", self.read(self.stub_log))
        self.assert_clean(out)

    def pf_up(self):
        return any("port-forward" in c for c in self.read(self.env["FAKE_KUBE_CALLS"]).splitlines())

    def test_s5_an_interrupt_during_the_poll_cleans_up(self):
        rc, out = self.interrupt(signal.SIGINT, lambda: "prompt" in self.read(self.stub_log), STUB_MODE="busy-forever")
        self.assertEqual(rc, 130, out)
        self.assert_clean(out)

    def test_s5_a_worker_whose_end_hook_cannot_push_is_kept_but_its_run_secret_is_removed(self):
        self.patch_state(hook_result={"pushed": False, "error": "push refused by the remote"})
        p, out = self.run_driver()
        self.assertEqual(p.returncode, cw.EXIT_HOOK, out)
        self.assertIn(self.name, self.st()["pods"])   # kept on purpose: the work is not lost
        self.assertIn("worker is kept", out)
        self.assert_clean(out, kept=True)

    def test_a_run_secret_that_cannot_be_deleted_is_said_to_remain_with_the_exact_command(self):
        self.patch_state(fail_cmd="delete", fail_cmd_only_run=True)
        p, out = self.run_driver(FAKE_KUBE_ECHO=GIT)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(len(re.findall(RUN % re.escape(self.name), out)), 1, out)
        self.assertRegex(out, r"per-run secret %s-run REMAINS: delete it with: \S*fake_kubectl --context acme-cw -n cw delete secret %s-run" % (self.name, self.name))
        self.assertNotIn("cleaned", out)
        self.assertEqual(os.listdir(self.scratch), [])
        self.assert_no_secret(self.everything(out))

    def test_a_failed_create_says_what_became_of_the_run_secret_once(self):
        self.patch_state(fail_cmd="label")   # after the run secret exists, before the pod
        p, out = self.run_driver(FAKE_KUBE_ECHO=" ".join([GIT] + MODEL_VALUES))
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.st().get("secrets", {}), {})
        self.assertEqual(len(re.findall(RUN % re.escape(self.name), out)), 1, out)
        self.assertEqual(os.listdir(self.scratch), [])
        self.assert_no_secret(self.everything(out))

    # -- the secrets never travel anywhere else --
    def test_no_secret_is_in_an_argument_an_environment_or_a_file_of_any_child(self):
        p, out = self.run_driver()
        self.assertEqual(p.returncode, 0, out)
        seen = [json.loads(x) for x in self.read(self.seen).splitlines()]
        self.assertTrue(seen)
        for s in seen:   # what each kubectl could see: its environment and its parent's (launcher's or driver's) command line
            self.assert_no_secret(json.dumps(s["env"]) + s["parent"], "child environment or parent argv")
        self.assertTrue(any("run_task.py" in s["parent"] or "cloud_worker.py" in s["parent"] for s in seen))
        self.assertNotIn("PASSWORD", " ".join(k for s in seen for k in s["env"] if k != "FAKE_SCAN_VALUES"))

    def test_every_form_of_a_planted_value_echoed_by_kubectl_is_scrubbed_from_the_drivers_output(self):
        encoded = []
        for v in MODEL_VALUES + [GIT]:
            raw = v.encode()
            encoded += [v, base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode().rstrip("="), raw.hex(),
                        urllib.parse.quote(v, safe=""), json.dumps(v)[1:-1]]
        self.patch_state(fail_cmd="port-forward")   # the port-forward dies at once, printing the echo: the driver shows its log
        p, out = self.run_driver(FAKE_KUBE_ECHO=" ".join(encoded))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("the port-forward ended", out)
        self.assert_no_secret(out)
        self.assertIn("[redacted]", out)
        self.assertEqual(self.st().get("secrets", {}), {})

    def test_the_driver_uses_the_launchers_one_scrub_function(self):
        with open(DRIVER) as f:
            src = f.read()
        self.assertNotIn("def scrub", src)
        self.assertNotIn("re.sub", src)
        sys.path.insert(0, os.path.dirname(HERE))
        import run_task
        self.assertIs(run_task.cw.scrub, cw.scrub)

    # -- inputs and replay --
    def test_a_missing_or_unreadable_input_refuses_and_creates_nothing(self):
        broken = os.path.join(self.tmp.name, "broken.json")
        with open(broken, "w") as f:
            f.write(json.dumps({"other-provider": {"key": OTHER}}))
        empty = os.path.join(self.tmp.name, "empty.txt")
        open(empty, "w").close()
        cases = {"auth file missing": {"CW_MODEL_AUTH_FILE": os.path.join(self.tmp.name, "missing.json")},
                 "no github-copilot entry": {"CW_MODEL_AUTH_FILE": broken},
                 "token command fails": {"CW_GIT_TOKEN_CMD": "false"},
                 "token command prints nothing": {"CW_GIT_TOKEN_CMD": "true"},
                 "token command missing": {"CW_GIT_TOKEN_CMD": "/nonexistent/cmd"}}
        for name, env in cases.items():
            with self.subTest(name):
                p, out = self.run_driver(**env)
                self.assertEqual(p.returncode, cw.EXIT_USAGE, out)
                self.assertIn("nothing was created", out)
                self.assertEqual((self.st().get("secrets", {}), self.st()["pods"]), ({}, {}))
                self.assertNotIn('"create"', self.read(self.env["FAKE_KUBE_CALLS"]))
                self.assertEqual(os.listdir(self.scratch), [])
                self.assertNotIn("per-run secret", out)
        for name, brief in (("brief missing", os.path.join(self.tmp.name, "none.txt")), ("brief empty", empty)):
            with self.subTest(name):
                p = subprocess.run(self.args(brief=brief), env=self.denv, capture_output=True, text=True)
                self.assertEqual(p.returncode, cw.EXIT_USAGE, p.stderr)
                self.assertEqual((self.st().get("secrets", {}), self.st()["pods"]), ({}, {}))

    def test_the_default_git_token_command_is_the_app_token_mint_for_the_repository(self):
        with open(DRIVER) as f:
            src = f.read()
        self.assertIn('os.path.join(HERE, "app_token.py"), self.a.repo', src)
        env = dict(self.denv)
        del env["CW_GIT_TOKEN_CMD"]
        env["CW_LAUNCHER"] = "/nonexistent"
        p = subprocess.run(self.args(), env=env, capture_output=True, text=True)
        if not os.path.exists(os.path.join(os.path.dirname(HERE), "app_token.py")):   # its own pull request may not be on this tree yet
            self.assertEqual(p.returncode, cw.EXIT_USAGE, p.stderr)
            self.assertIn("nothing was created", p.stderr)

    def test_the_target_repository_is_an_argument_with_no_default(self):
        for argv in ([sys.executable, DRIVER, "coder7"], [sys.executable, DRIVER, "coder7", "main", self.brief],
                     [sys.executable, DRIVER, "coder7", "not-a-repo", "main", self.brief], [sys.executable, DRIVER, "coder7", "a/b/c", "main", self.brief]):
            p = subprocess.run(argv, env=self.denv, capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, argv)
        self.assertFalse(os.path.exists(self.git_ran))

    def test_a_replayed_run_refuses_and_leaves_the_first_run_alone(self):
        first = self.create("coder7", "--run-secret-fd", "model-auth=%d" % self.fd(MODEL_VALUES[0]))
        before = self.st()
        self.assertIn(self.name, before["pods"])
        p, out = self.run_driver()
        self.assertEqual(p.returncode, cw.EXIT_REFUSED, out)
        self.assertIn("already exists", out)
        self.assertEqual(self.st(), before)   # the first run's pod and Secrets are untouched, nothing was deleted or added
        self.assertNotIn("per-run secret", out)
        self.assertFalse(os.path.exists(self.git_ran), "the token was minted for a run that was refused")

    def fd(self, value):
        r, w = os.pipe()
        os.write(w, value.encode())
        os.close(w)
        self.addCleanup(os.close, r)
        return r


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if result.wasSuccessful() else 1)
