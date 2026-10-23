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
sys.path.insert(0, os.path.join(HERE, "..", "..", "worker", "tests"))
import stub_support  # noqa: E402
import test_cloud_worker as base  # noqa: E402
cw = base.cw

DRIVER = os.path.join(os.path.dirname(HERE), "run_task.py")
FAKE_PREFLIGHT_DRIVER = os.path.join(HERE, "fake_preflight", "run_task.py")   # the driver with a passing fake preflight (#665): the real one cannot run offline
REPO = "acme-org/widget-service"
MODEL_VALUES = ["ab12CD34e", "modelvalue-Qx7Lm2Qx7Lm2Qx7Lm2", "m" * 7 + "-" + "Zk9Wp3" * 12]   # short, medium, long
OTHER = "othervalue-Zk9Wp3Zk9Wp3Zk9Wp3Zk9Wp3"   # another provider in the same auth file: never read out
GIT = "gitvalue-Rt4Vb8Rt4Vb8Rt4Vb8Rt4Vb8"
ENTRY = {"type": "oauth", "refresh": MODEL_VALUES[0], "access": MODEL_VALUES[1], "extra": MODEL_VALUES[2]}
ROW = {"id": "id-copilot", "integrationID": "github-copilot", "label": "lbl-x7q2", "active": True, "value": ENTRY}   # one entry of `opencode auth export`
OTHER_ROW = {"id": "id-other", "integrationID": "other-provider", "label": "lbl-o3k9", "active": True, "value": {"type": "api", "key": OTHER}}
FAKE_EXPORT = os.path.join(HERE, "fake_opencode_export.sh")
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
        self.export = os.path.join(d, "export.json")   # what the stand-in `opencode auth export` prints (it reads $HOME/export.json)
        with open(self.export, "w") as f:
            json.dump([OTHER_ROW, ROW], f)
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
        self.denv = dict({"PATH": os.environ["PATH"], "HOME": d}, **self.env, TMPDIR=self.scratch, CW_TMPDIR=self.scratch, CW_OPENCODE_BIN=FAKE_EXPORT,
                         CW_GIT_TOKEN_CMD=self.gitcmd, FAKE_SCAN_DIR=self.scratch, FAKE_KUBE_SEEN=self.seen,
                         FAKE_SCAN_VALUES=",".join(MODEL_VALUES + [GIT, OTHER, json.dumps([ROW])]),
                         STUB_LOG=self.stub_log, CW_POLL_SECONDS="0.2", CW_HEALTH_TIMEOUT="3", CW_RUN_TIMEOUT="60", CW_CREATE_TIMEOUT="60")

    def args(self, worker="coder7", repo=REPO, brief=None):
        return [sys.executable, FAKE_PREFLIGHT_DRIVER, worker, repo, "main", brief or self.brief, "--pull-secret", "cw-pull", "--image", "registry.example.internal/cw-worker:1"]

    def run_driver(self, **env):
        p = subprocess.run(self.args(), env=dict(self.denv, **env), capture_output=True, text=True, timeout=120)
        return p, p.stdout + p.stderr

    def spawn(self, **env):
        return stub_support.start(self, self.args(), env=dict(self.denv, **env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                preexec_fn=lambda: [signal.signal(x, signal.SIG_DFL) for x in (signal.SIGINT, signal.SIGTERM)])   # a runner that started us with SIGINT ignored must not hide the interrupt

    def read(self, path):
        if not os.path.exists(path):
            return ""
        with open(path, errors="replace") as f:
            return f.read()

    def everything(self, outputs):
        return "\n".join([outputs, self.read(self.state + ".calls"), self.read(self.env["FAKE_KUBE_CALLS"]), self.read(self.seen),
                          self.read(self.env["CW_CLOUD_AUDIT"])])

    def assert_no_secret(self, text, what="output"):
        for v in MODEL_VALUES + [GIT, OTHER, json.dumps([ROW])]:
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
        self.assertIn(base64.b64encode(json.dumps([ROW]).encode()).decode(), sent)
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
        empty = os.path.join(self.tmp.name, "empty.txt")
        open(empty, "w").close()
        cases = {"export command missing": {"CW_OPENCODE_BIN": "/nonexistent/opencode"},
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

    def test_every_export_refusal_exits_2_by_name_creates_nothing_and_never_prints_the_credential(self):
        """#1088 S3, U3, S8 through the real driver, one subtest per member of the refusal table of test_model_credential."""
        import test_model_credential as tmc
        canary_forms = [f for v in (tmc.CANARY, tmc.CANARY + "-r") for f in forms(v)]
        for case, (text, code, word) in tmc.REFUSALS.items():
            with self.subTest(case):
                with open(self.export, "w") as f:
                    f.write(text)
                exit_file = os.path.join(self.tmp.name, "export.exit")
                if code:
                    open(exit_file, "w").write(str(code))
                try:
                    p, out = self.run_driver()
                finally:
                    if os.path.exists(exit_file):
                        os.remove(exit_file)
                self.assertEqual(p.returncode, cw.EXIT_USAGE, out)
                self.assertIn("github-copilot", out)
                self.assertIn(word, out)
                self.assertEqual((self.st().get("secrets", {}), self.st()["pods"]), ({}, {}))
                self.assertNotIn('"create"', self.read(self.env["FAKE_KUBE_CALLS"]))
                self.assertEqual(os.listdir(self.scratch), [])
                for form in canary_forms:
                    self.assertNotIn(form, out)

    def test_the_export_command_runs_with_the_fixed_argv_and_no_cluster_or_token_variable(self):
        p, out = self.run_driver(CW_CLUSTER_TOKEN_CMD="true", GH_TOKEN="g", GITHUB_TOKEN="h")
        self.assertEqual(p.returncode, 0, out)
        d = self.tmp.name
        self.assertEqual(self.read(os.path.join(d, "oc-argv")).strip(), "auth export")
        names = {l.split("=", 1)[0] for l in self.read(os.path.join(d, "oc-env")).splitlines() if "=" in l} - {"PWD", "SHLVL", "_", "OLDPWD"}
        self.assertEqual({n for n in names if not (n in ("PATH", "HOME", "LANG") or n.startswith("XDG_"))}, set())

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

    # -- the race: a run that loses the name changes nothing --
    def other_run_script(self, name, wait_for_pod=False):
        """A token command that stands in for the OTHER run: the slow step in which it makes the worker (pod and both Secrets,
        another run id) in the cluster state, and snapshots the state it made. `wait_for_pod`: it waits instead for a real
        run to have made the pod, then mints the token."""
        snap = os.path.join(self.tmp.name, "other-snapshot.json")
        code = (
            "import json, os, sys, time\n"
            "path, snap, name, wait = os.environ['FAKE_KUBE_STATE'], %r, %r, %r\n"
            "if wait:\n"
            "    for _ in range(300):\n"
            "        if name in json.load(open(path)).get('pods', {}): break\n"
            "        time.sleep(0.1)\n"
            "    time.sleep(0.3)\n"
            "else:\n"
            "    st = json.load(open(path))\n"
            "    st['pods'][name] = {'metadata': {'name': name, 'annotations': {'cw/run-id': 'feedface00000000'}}, 'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}\n"
            "    st['secrets'] = {name + '-pw': 1, name + '-run': 1}\n"
            "    st['secret_labels'] = {name + '-run': {'cw/run-id': 'feedface00000000'}}\n"
            "    json.dump(st, open(path, 'w'))\n"
            "    json.dump(st, open(snap, 'w'))\n"
            "sys.stdout.write(%r)\n" % (snap, name, wait_for_pod, GIT))
        script = os.path.join(self.tmp.name, "other-run.py")
        with open(script, "w") as f:
            f.write(code)
        return "%s %s" % (sys.executable, script), snap

    def calls(self):
        return [json.loads(x) for x in self.read(self.env["FAKE_KUBE_CALLS"]).splitlines()]

    def test_a_pod_that_appears_between_the_check_and_the_create_is_left_whole(self):
        cmd, snap = self.other_run_script(self.name)
        p, out = self.run_driver(CW_GIT_TOKEN_CMD=cmd)
        self.assertEqual(p.returncode, cw.EXIT_REFUSED, out)
        self.assertIn("already exists", out)
        with open(snap) as f:
            self.assertEqual(self.st(), json.load(f))   # the other run's pod and both Secrets: nothing added, nothing deleted
        for forbidden in ("delete", "exec", "annotate", "patch", "label", "apply"):
            self.assertNotIn(forbidden, [c[4] for c in self.calls() if c[0] == "--context"], "the loser touched the cluster: " + forbidden)
        self.assertNotIn("per-run secret", out.replace("belongs to another run", ""))   # it made none, so it says nothing of one
        self.assertIn("belongs to another run", out)
        self.assertEqual(os.listdir(self.scratch), [])
        self.assert_no_secret(self.everything(out))

    def test_the_launcher_says_already_exists_with_its_own_exit_code_and_makes_nothing(self):
        self.create("coder7", "--run-secret-fd", "model-auth=%d" % self.fd(MODEL_VALUES[0]))
        before = self.st()
        code, out, err = self.run_cli("create", "coder7", "--repo", REPO, "--branch", "main", "--run-secret-fd", "model-auth=%d" % self.fd(MODEL_VALUES[1]))
        self.assertEqual(code, cw.EXIT_EXISTS)
        self.assertIn("already exists", err)
        self.assertEqual(self.st(), before)

    def test_two_drivers_racing_for_one_name_one_wins_and_the_loser_changes_nothing(self):
        wait_cmd, _ = self.other_run_script(self.name, wait_for_pod=True)
        winner = self.spawn(STUB_MODE="busy-forever")
        end = time.time() + 40
        while time.time() < end and not self.pf_up():
            time.sleep(0.1)
        self.assertTrue(self.pf_up(), "the first run never got going")
        # the second run passed its own first check before the first made the pod (its token step is the slow one)
        loser_args = self.args()
        slow = os.path.join(self.tmp.name, "slow-token")
        with open(slow, "w") as f:
            f.write("#!/bin/sh\nsleep 0.5\nprintf '%%s' '%s'\n" % GIT)
        os.chmod(slow, 0o700)
        st_mid, calls_mid = self.st(), len(self.calls())
        p = subprocess.run(loser_args, env=dict(self.denv, CW_GIT_TOKEN_CMD=slow), capture_output=True, text=True, timeout=120)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, cw.EXIT_REFUSED, out)
        self.assertIn("already exists", out)
        after = self.st()
        self.assertEqual(after["pods"], st_mid["pods"])      # the winner's pod and its Secrets are whole
        self.assertEqual(after["secrets"], st_mid["secrets"])
        mutating = [c for c in self.calls()[calls_mid:] if c[0] == "--context" and c[4] in ("delete", "exec", "apply", "create", "patch", "label", "annotate")]
        self.assertEqual(mutating, [], "the loser changed the cluster")
        winner.send_signal(signal.SIGINT)
        wout, werr = winner.communicate(timeout=120)
        self.assertEqual(winner.returncode, 130, wout + werr)
        self.assertEqual(self.st().get("secrets", {}), {})   # only now, by the winner's own cleanup, is everything gone
        self.assertEqual(self.st()["pods"], {})

    def test_every_secret_and_the_pod_of_a_run_carry_its_nonce_and_the_driver_reads_it_back(self):
        proc = self.spawn(STUB_MODE="busy-forever")
        end = time.time() + 40
        while time.time() < end and not self.pf_up():
            time.sleep(0.1)
        st = self.st()
        nonce = st["pods"][self.name]["metadata"]["annotations"]["cw/run-id"]
        self.assertRegex(nonce, r"^[0-9a-f]{16}$")
        self.assertEqual(st["secret_labels"][self.name + "-pw"]["cw/run-id"], nonce)
        self.assertEqual(st["secret_labels"][self.name + "-run"]["cw/run-id"], nonce)
        self.assertTrue(any(c[-2:] == ["get", "pod"] or "get" in c for c in self.calls()))
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=120)

    # -- cleanup decides only on proof: unit cases with a stub cluster --
    def stub_driver(self, pod=None, secret=None, boom=False):
        sys.path.insert(0, os.path.dirname(HERE))
        import run_task
        calls = []

        class K:
            def get_pod(s, n):
                if boom: raise cw.CloudError("kubectl get: odd answer")
                return pod
            def get_run_secret(s, n):
                if boom: raise cw.CloudError("kubectl get: odd answer")
                return secret
            def delete(s, n): calls.append(("delete", n))
            def delete_run_secret(s, n): calls.append(("delete-run", n))
            def delete_command(s, k, o): return "kubectl delete %s %s" % (k, o)
            def scrub(s, t, limit=0): return t
            def close(s): pass
        a = type("A", (), {"worker": "coder7"})()
        d = run_task.Driver(a, {})
        d.kube, d.create_started = K(), True
        d.stop = lambda reason: calls.append(("stop", reason)) or True
        return d, calls

    def quiet(self, d):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
            d.cleanup()
        return buf.getvalue()

    def test_cleanup_leaves_a_pod_with_a_missing_or_other_run_id_and_runs_no_end_hook(self):
        for label, meta in (("other id", {"annotations": {"cw/run-id": "feedface00000000"}}), ("no annotation", {}), ("odd type", {"annotations": None})):
            with self.subTest(label):
                d, calls = self.stub_driver(pod={"metadata": meta})
                out = self.quiet(d)
                self.assertEqual(calls, [])
                self.assertIn("left whole", out)
                self.assertNotIn("cleaned", out)

    def test_cleanup_stops_a_pod_only_when_the_read_back_nonce_matches(self):
        d, calls = self.stub_driver()
        d.kube.get_pod = lambda n: {"metadata": {"annotations": {"cw/run-id": d.nonce}}}
        out = self.quiet(d)
        self.assertEqual([c[0] for c in calls], ["stop", "delete-run"])
        self.assertIn("per-run secret %s-run cleaned" % d.name, out)

    def test_cleanup_with_no_pod_deletes_a_run_secret_only_when_it_carries_the_nonce(self):
        d, calls = self.stub_driver(secret={"metadata": {"labels": {"cw/run-id": "feedface00000000"}}})
        self.quiet(d)
        self.assertEqual(calls, [])
        d, calls = self.stub_driver()
        d.kube.get_run_secret = lambda n: {"metadata": {"labels": {"cw/run-id": d.nonce}}}
        self.assertIn("cleaned", self.quiet(d))
        self.assertEqual(calls, [("delete", d.name)])
        d, calls = self.stub_driver()   # nothing there at all: nothing to do and nothing said of a secret
        self.assertNotIn("per-run secret", self.quiet(d))
        self.assertEqual(calls, [])

    def test_cleanup_that_cannot_read_ownership_touches_nothing_and_says_how_to_delete(self):
        d, calls = self.stub_driver(boom=True)
        out = self.quiet(d)
        self.assertEqual(calls, [])
        self.assertIn("REMAINS", out)
        self.assertIn("kubectl delete secret %s-run" % d.name, out)

    # -- the create itself is exclusive: of two callers exactly one wins, and a loser never deletes the winner's objects --
    A_ID, B_ID = "aaaaaaaa11111111", "bbbbbbbb22222222"

    def loser_create(self, run_id=None, **patches):
        """A second launcher call that passed its own first check (the check is made to see nothing, as in the race)."""
        real = cw.Kube.get_pod
        seen = []

        def get_pod(kube, name):
            seen.append(name)
            return None if len(seen) == 1 else real(kube, name)
        patches = dict(patches, get_pod=get_pod)
        olds = {k: getattr(cw.Kube, k) for k in patches}
        for k, v in patches.items():
            setattr(cw.Kube, k, v)
        try:
            return self.run_cli("create", "coder7", "--repo", REPO, "--branch", "main", "--run-id", run_id or self.B_ID,
                                "--run-secret-fd", "model-auth=%d" % self.fd(MODEL_VALUES[1]))
        finally:
            for k, v in olds.items():
                setattr(cw.Kube, k, v)

    def test_the_second_create_fails_after_the_first_wins_and_the_winners_objects_are_untouched(self):
        self.create("coder7", "--run-id", self.A_ID, "--run-secret-fd", "model-auth=%d" % self.fd(MODEL_VALUES[0]))
        before = self.st()
        code, out, err = self.loser_create()
        self.assertEqual(code, cw.EXIT_EXISTS, err)
        self.assertIn("already exists", err)
        self.assertEqual(self.st(), before)   # password Secret, run Secret and pod: not replaced, not deleted
        self.assertEqual(before["pods"][self.name]["metadata"]["annotations"]["cw/run-id"], self.A_ID)
        self.assertNotIn("CLEANUP", err)

    def test_a_loser_that_made_its_own_secrets_before_the_pod_clash_deletes_only_those(self):
        self.patch_state(pods={self.name: {"metadata": {"name": self.name, "annotations": {"cw/run-id": self.A_ID}}, "status": {}}})
        code, out, err = self.loser_create()
        self.assertEqual(code, cw.EXIT_EXISTS, err)
        st = self.st()
        self.assertEqual(st.get("secrets", {}), {})   # its own two Secrets (labelled with its id) are gone
        self.assertIn(self.name, st["pods"])         # the other run's pod is whole
        self.assertEqual(st["pods"][self.name]["metadata"]["annotations"]["cw/run-id"], self.A_ID)

    def test_a_failed_create_never_deletes_an_object_that_carries_another_run_id(self):
        first = self.create("coder7", "--run-id", self.A_ID, "--run-secret-fd", "model-auth=%d" % self.fd(MODEL_VALUES[0]))
        before = self.st()

        def boom(kube, obj):   # the pod create fails for another reason than AlreadyExists, after the objects are flagged made
            raise cw.CloudError("kubectl create: quota exceeded")
        code, out, err = self.loser_create(apply=boom, create_run_secret=lambda *a, **k: None)
        self.assertNotEqual(code, cw.EXIT_EXISTS)
        after = self.st()
        self.assertEqual(after["pods"], before["pods"])
        self.assertEqual(after["secrets"], before["secrets"])   # the winner's Secrets and pod are whole
        self.assertIn("left whole", err)
        self.assertNotIn("cleaned", err.replace("left whole", ""))

    def test_an_unreadable_object_during_the_launchers_cleanup_is_left_and_reported_not_deleted(self):
        self.create("coder7", "--run-id", self.A_ID, "--run-secret-fd", "model-auth=%d" % self.fd(MODEL_VALUES[0]))
        before = self.st()

        def boom(kube, obj):
            raise cw.CloudError("kubectl create: quota exceeded")

        def odd(kube, name):
            raise cw.CloudError("kubectl get: odd answer")
        code, out, err = self.loser_create(apply=boom, get_secret=odd, create_run_secret=lambda *a, **k: None)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.st(), before)
        self.assertIn("REMAINS", err)

    def test_a_run_secret_whose_label_step_fails_is_removed_by_the_launcher_not_left_unlabelled(self):
        self.patch_state(fail_cmd="label")
        code, out, err = self.run_cli("create", "coder7", "--repo", REPO, "--branch", "main", "--run-id", self.B_ID,
                                      "--run-secret-fd", "model-auth=%d" % self.fd(MODEL_VALUES[1]))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.st().get("secrets", {}), {})

    def test_a_run_secret_with_no_run_id_is_unknown_to_the_driver_and_reported_as_remaining(self):
        d, calls = self.stub_driver(secret={"metadata": {"labels": {"cw/pod": "x"}}})
        out = self.quiet(d)
        self.assertEqual(calls, [])
        self.assertIn("REMAINS", out)
        self.assertIn("delete it with", out)

    def test_two_drivers_started_together_exactly_one_wins(self):
        gate = os.path.join(self.tmp.name, "gate")
        os.mkdir(gate)
        tok = os.path.join(self.tmp.name, "gate-token")
        with open(tok, "w") as f:   # each driver announces itself, then waits until both have: both then create at once
            f.write("#!/bin/sh\ntouch \"%s/$$\"\nwhile [ $(ls \"%s\" | wc -l) -lt 2 ]; do sleep 0.05; done\nprintf '%%s' '%s'\n" % (gate, gate, GIT))
        os.chmod(tok, 0o700)
        procs = [self.spawn(CW_GIT_TOKEN_CMD=tok) for _ in range(2)]
        results = []
        for pr in procs:
            out, err = pr.communicate(timeout=120)
            results.append((pr.returncode, out + err))
        codes = sorted(r[0] for r in results)
        self.assertEqual(codes, [0, cw.EXIT_REFUSED], results)
        loser = [o for c, o in results if c != 0][0]
        self.assertIn("already exists", loser)
        self.assertNotIn("stopped", loser)
        winner = [o for c, o in results if c == 0][0]
        self.assertIn("per-run secret %s-run cleaned" % self.name, winner)
        calls = self.calls()
        hooks = [c for c in calls if c[0] == "--context" and c[4] == "exec" and "cw-end-hook" in c]   # the run record's read-only git rev-parse is not a hook
        self.assertEqual(len(hooks), 1, "the end hook ran more than once")
        self.assertEqual(self.st().get("secrets", {}), {})
        self.assertEqual(self.st()["pods"], {})

    def fd(self, value):
        r, w = os.pipe()
        os.write(w, value.encode())
        os.close(w)
        self.addCleanup(os.close, r)
        return r


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if result.wasSuccessful() else 1)
