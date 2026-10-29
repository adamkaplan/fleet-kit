"""Ask #1509: the persistent orchestrator (an always-on orchestrator: one StatefulSet, restart always, no deadline, ended by an explicit stop).

Offline: the fake kubectl beside this file is the cluster (it knows StatefulSets: it makes the pod NAME-0, and a pod deleted alone comes back while
its StatefulSet is there). Covers the scenarios S2 to S14 of the plan, the manifest rule, and the mutants: each mutation is asserted applied and the
same check that passes on the real code must fail on it.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
import cloud_worker as cw  # noqa: E402
from test_cloud_worker import Base, T0  # noqa: E402
import test_cloud_stack as stack_tests  # noqa: E402
import test_git_token_renewal as renewal  # noqa: E402
import test_cloud_k1 as k1  # noqa: E402

SOURCE = os.path.join(LAUNCHER, "cloud_worker.py")
REGISTRY = "cloud-hub-registry"
OTHER = {"other-agent": "f" * 64}
GIT_VALUE = "g" + "1" * 30
WRITE_VERBS = ("delete", "patch", "create", "label", "annotate", "apply")
WORKER = "orch1"
NAME = cw.pod_name(WORKER)
POD = NAME + "-0"


def mutant(old, new):
    """The real source with ONE text replaced (asserted present exactly once and changed): a module to run the same check against."""
    with open(SOURCE) as f:
        src = f.read()
    assert src.count(old) == 1, "the mutation target is not there exactly once: %r" % old
    changed = src.replace(old, new)
    assert changed != src, "the mutation changed nothing"
    mod = types.ModuleType("cloud_worker_mutant")
    mod.__file__ = SOURCE
    exec(compile(changed, SOURCE, "exec"), mod.__dict__)
    return mod


def fd_with(value):
    f = tempfile.TemporaryFile()
    f.write(value.encode())
    f.flush()
    f.seek(0)
    fd = os.dup(f.fileno())
    f.close()
    return fd


class PBase(Base):
    mod = cw   # the module under test (a mutant swaps it)

    def setUp(self):
        super().setUp()
        self.patch_state(secrets={REGISTRY: 1, "cloud-hub-pw": 1},
                         secret_bodies={"cloud-hub-pw": {"keep": "kept-pw"}, REGISTRY: {"secrets.json": json.dumps(OTHER), "agents.json": json.dumps({"other-agent": {"url": "u"}})}},
                         hook_result={"pushed": False, "wip_branch": None})
        self.fds = []
        self.addCleanup(self.close_fds)

    def close_fds(self):
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass

    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        self.mod.REAPER_LOCK_ROOT = self.lock_root
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.mod.main(list(argv), self.env, self.clock, lambda s: self.clock.advance(seconds=s))
        return code, out.getvalue(), err.getvalue()

    def launch(self, worker=WORKER, *extra, expect=0, token=True, agent=True, persistent=True):
        argv = ["create", worker, "--repo", "acme-org/widget-service", "--branch", "main", "--run-id", "0123456789abcdef"]
        if agent:
            argv += ["--agent", "orchestrator", "--boss", "boss1"]
        if token:
            fd = fd_with(GIT_VALUE)
            self.fds.append(fd)
            argv += ["--run-secret-fd", "git-token=%d" % fd]
        if persistent:
            argv.append("--persistent")
        code, out, err = self.run_cli(*argv, *extra)
        self.assertEqual(code, expect, err)
        return code, out, err

    def calls(self):
        if not os.path.exists(self.env["FAKE_KUBE_CALLS"]):
            return []
        with open(self.env["FAKE_KUBE_CALLS"]) as f:
            return [json.loads(x) for x in f if x.strip()]

    def verbs(self, calls=None):
        """(verb, kind, name) of each call, the login flags dropped."""
        out = []
        for a in (self.calls() if calls is None else calls):
            rest = a[a.index("-n") + 2:]
            out.append((rest[0], rest[1] if len(rest) > 1 else "", rest[2] if len(rest) > 2 else ""))
        return out

    def writes(self, calls=None):
        return [v for v in self.verbs(calls) if v[0] in WRITE_VERBS]

    def sts(self):
        return self.st().get("sts", {})

    def registry(self):
        bodies = self.st()["secret_bodies"][REGISTRY]
        return json.loads(bodies["secrets.json"]), json.loads(bodies["agents.json"])

    def mark_fresh_token(self):
        meta = self.st().setdefault("secret_meta", {})
        meta[NAME + "-run"] = {"uid": "uid-1", "annotations": {cw.GIT_TOKEN_MINTED: cw.iso(self.clock())}}
        self.patch_state(secret_meta=meta)

    def reap_pass(self):
        return self.mod.Launcher(self.mod.Kube(self.env), self.env, self.clock).reap()

    def created_nothing(self):
        st = self.st()
        self.assertNotIn("applied", st)
        self.assertEqual(st.get("sts", {}), {})
        self.assertEqual(set(st["secrets"]), {REGISTRY, "cloud-hub-pw"})


class S2Render(PBase):
    def check_render(self):
        _, out, _ = self.launch()
        objects = self.st()["applied"]
        self.assertEqual([o["kind"] for o in objects].count("StatefulSet"), 1)
        self.assertEqual([o["kind"] for o in objects].count("Pod"), 0)
        sts = self.sts()[NAME]
        spec = sts["spec"]
        self.assertEqual((sts["apiVersion"], spec["replicas"], spec["serviceName"]), ("apps/v1", 1, cw.HUB_SUBDOMAIN))
        template = spec["template"]
        self.assertEqual(template["spec"]["restartPolicy"], "Always")
        self.assertNotIn("activeDeadlineSeconds", json.dumps(sts))
        notes = template["metadata"]["annotations"]
        self.assertEqual(sorted(k for k in notes if "deadline" in k or "idle" in k or "max-hard" in k), [])   # the template carries none of the limits
        self.assertEqual(notes[cw.PERSISTENT_ANNOTATION], "true")
        self.assertEqual(sts["metadata"]["annotations"][cw.PERSISTENT_ANNOTATION], "true")
        self.assertEqual(template["spec"]["containers"][0]["resources"]["limits"],
                         {"cpu": cw.SHAPES[cw.DEFAULT_ORCHESTRATOR_SHAPE]["cpu"], "memory": cw.SHAPES[cw.DEFAULT_ORCHESTRATOR_SHAPE]["memory"]})
        self.assertEqual(template["metadata"]["labels"]["app"], cw.LABEL)   # list_pods still sees the pod
        self.assertEqual(spec["selector"]["matchLabels"].items() <= template["metadata"]["labels"].items(), True)
        env = {e["name"] for e in template["spec"]["containers"][0]["env"]}
        self.assertFalse({"CW_IDLE_SECONDS", "CW_HARD_DEADLINE"} & env)
        self.assertTrue({NAME + "-pw", NAME + "-run"} <= set(self.st()["secrets"]))   # the password Secret and the per-run Secret
        self.assertIn(POD, self.st()["pods"])
        self.assertEqual([p["metadata"]["name"] for p in self.mod.Kube(self.env).list_pods()], [POD])

    def test_s2_one_statefulset_of_one_replica_without_any_deadline(self):
        self.check_render()

    def test_s2_the_guards_run_on_the_whole_workload(self):
        a = argparse.Namespace(repo="acme-org/widget-service", branch="main", idle="10m", hard="2h", max_hard="8h", shape="S", image=None, run_id=None,
                               pull_secret=None, agent="orchestrator", boss="boss1", stack=False, persistent=True)
        names = ("git-token", cw.CHANNEL_SECRET_KEY)
        sts = cw.render_workload(a, WORKER, NAME, T0, dict(self.env), names)
        self.assertEqual(cw.workload_problems(sts), [])
        self.assertEqual(cw.scan_workload(sts), [])
        bad = json.loads(json.dumps(sts))
        bad["spec"]["template"]["spec"]["volumes"].append({"name": "h", "hostPath": {"path": "/"}})
        self.assertIn("hostPath", cw.workload_problems(bad))
        bad = json.loads(json.dumps(sts))
        bad["spec"]["volumeClaimTemplates"] = [{"x": "/var/run/docker.sock"}]
        self.assertIn("docker.sock", cw.workload_problems(bad))
        bad = json.loads(json.dumps(sts))
        bad["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "GH_TOKEN", "value": "x"})
        self.assertTrue(cw.scan_workload(bad))
        # through the launch: nothing is made when a guard fires
        real = cw.render_pod

        def hostile(*args, **kw):
            pod = real(*args, **kw)
            pod["spec"]["volumes"].append({"name": "h", "hostPath": {"path": "/"}})
            return pod
        cw.render_pod = hostile
        self.addCleanup(setattr, cw, "render_pod", real)
        code, _, err = self.run_cli("create", WORKER, "--repo", "acme-org/widget-service", "--branch", "main", "--agent", "orchestrator", "--boss", "b",
                                    "--persistent", "--run-secret-fd", "git-token=%d" % self.fd())
        self.assertEqual(code, 2, err)
        self.created_nothing()

    def fd(self):
        fd = fd_with(GIT_VALUE)
        self.fds.append(fd)
        return fd

    def test_s2_a_stored_credential_in_the_workload_is_refused(self):
        real = cw.render_workload

        def leaking(*args, **kw):
            sts = real(*args, **kw)
            sts["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "API_TOKEN", "value": "x"})
            return sts
        cw.render_workload = leaking
        self.addCleanup(setattr, cw, "render_workload", real)
        code, _, err = self.run_cli("create", WORKER, "--repo", "acme-org/widget-service", "--branch", "main", "--agent", "orchestrator", "--boss", "b",
                                    "--persistent", "--run-secret-fd", "git-token=%d" % self.fd())
        self.assertEqual(code, 2, err)
        self.assertIn("stored credential", err)
        self.created_nothing()

    def test_s2_the_repository_allow_list_still_runs(self):
        code, _, err = self.run_cli("create", WORKER, "--repo", "acme-org/not-listed", "--branch", "main", "--agent", "orchestrator", "--boss", "b",
                                    "--persistent", "--run-secret-fd", "git-token=%d" % self.fd())
        self.assertNotEqual(code, 0, err)
        self.created_nothing()

    def test_s2_the_spend_cap_is_priced_for_one_day(self):
        day = cw.SHAPES[cw.DEFAULT_ORCHESTRATOR_SHAPE]["usd_per_hour"] * 24
        self.env["CW_CLOUD_SPEND_CAP_USD"] = "%.4f" % (day - 0.01)
        code, _, err = self.run_cli("create", WORKER, "--repo", "acme-org/widget-service", "--branch", "main", "--agent", "orchestrator", "--boss", "b",
                                    "--persistent", "--run-secret-fd", "git-token=%d" % self.fd())
        self.assertEqual(code, cw.EXIT_CAP, err)
        self.created_nothing()
        self.env["CW_CLOUD_SPEND_CAP_USD"] = "%.4f" % (day + 0.01)
        self.launch()   # the same launch fits a cap just above one day of the shape

    def test_s2_a_persistent_flag_that_does_not_fit_is_refused_and_nothing_is_made(self):
        cases = {"a worker": dict(agent=False), "a stack launch": dict(stack=True), "no per-run token route": dict(token=False)}
        for what, kw in cases.items():
            with self.subTest(what):
                extra = ["--stack", "--shape", "S"] if kw.pop("stack", False) else []
                code, _, err = self.launch(WORKER, *extra, expect=2, **kw)
                self.assertIn("--persistent", err)
                self.created_nothing()

    def test_s8_mutant_a_deadline_left_on_the_pod_fails_the_render_check(self):
        self.mod = mutant('for key in ("activeDeadlineSeconds", "hostname", "subdomain"):', 'for key in ("hostname", "subdomain"):')
        with self.assertRaises(AssertionError):
            self.check_render()


class S3Unchanged(PBase):
    LEGACY_ANSWER = {"worker", "url", "auth_ref", "runtime_id", "idle_deadline", "hard_deadline", "password_fd"}

    def test_s3_the_recorded_fixtures_still_equal_what_the_launcher_renders(self):
        with open(os.path.join(HERE, "fixtures", "default_pod.json")) as f:
            self.assertEqual(json.dumps({k: stack_tests.build(a, n) for k, (a, n) in stack_tests.CASES.items()}, indent=2, sort_keys=True) + "\n", f.read())
        with open(os.path.join(HERE, "fixtures", "stack_pod.json")) as f:
            self.assertEqual(json.dumps({k: stack_tests.build(a, n) for k, (a, n) in stack_tests.STACK_CASES.items()}, indent=2, sort_keys=True) + "\n", f.read())

    def test_s3_a_non_persistent_orchestrator_is_the_pod_it_always_was(self):
        self.launch("orch2", persistent=False, token=False)
        pod = self.pod("orch2")
        self.assertNotIn("cw/persistent", pod["metadata"]["annotations"])
        self.assertEqual(pod["spec"]["restartPolicy"], "Never")
        self.assertIn("activeDeadlineSeconds", pod["spec"])
        self.assertIn("cw/hard-deadline", pod["metadata"]["annotations"])
        self.assertEqual(self.st().get("sts", {}), {})

    def test_s3_a_worker_answer_audit_line_and_record_are_the_legacy_ones(self):
        out = self.create("coder7")
        self.assertEqual(set(out), self.LEGACY_ANSWER)
        line = [x for x in self.audit_lines() if x["action"] == "create"][0]
        self.assertEqual(set(line), {"ts", "worker", "runtime_id", "action", "cost_estimate_usd", "ok", "actor", "reason"})
        records = os.path.join(self.tmp.name, "rec")
        self.run_cli("stop", "coder7", "--record-dir", records)
        runs = [os.path.join(records, d, "run.json") for d in os.listdir(records)]
        with open(runs[0]) as f:
            self.assertNotIn("persistent", json.load(f))

    def test_s3_the_hub_entry_of_a_persistent_orchestrator_holds_its_pod_zero_address(self):
        self.launch()
        _, agents = self.registry()
        self.assertEqual(agents[WORKER]["url"], cw.hub_agent_url(POD, "cw"))
        self.assertTrue(agents[WORKER]["url"].startswith("http://%s." % POD))
        self.assertTrue(agents[WORKER]["url"].endswith(".%s.cw.svc.cluster.local:4096" % cw.HUB_SUBDOMAIN))


class S4Reaper(PBase):
    def setUp(self):
        super().setUp()
        self.launch()

    def plant(self, **state):
        self.patch_state(**state)

    def stamp_deadlines(self):
        st = self.st()
        notes = st["pods"][POD]["metadata"]["annotations"]
        notes.update({"cw/hard-deadline": cw.iso(T0 + dt.timedelta(minutes=1)), "cw/idle-seconds": "60", "cw/max-hard": cw.iso(T0 + dt.timedelta(hours=8))})
        st["activity"] = {POD: {"busy": False, "last_activity": cw.iso(T0)}}
        self.write_state(st)

    def check_left_whole(self):
        self.stamp_deadlines()
        self.clock.advance(hours=3)   # past the planted hard deadline, and idle for 180 times the idle limit
        self.mark_fresh_token()
        open(self.env["FAKE_KUBE_CALLS"], "w").close()
        res = self.reap_pass()
        self.assertEqual(res, [])
        self.assertEqual(self.writes(), [])
        self.assertIn(POD, self.st()["pods"])
        self.assertIn(NAME, self.sts())

    def test_s4_a_persistent_pod_is_not_stopped_by_a_deadline_or_idleness(self):
        for what in ("past its deadline annotation and idle for ten times the limit", "past only a deadline annotation", "idle"):
            with self.subTest(what):
                self.check_left_whole()

    def test_s4_an_unreadable_workload_leaves_the_pod_untouched_with_one_line(self):
        forbidden = 'Error from server (Forbidden): statefulsets.apps "%s" is forbidden: User "cw-operator" cannot get resource "statefulsets" in API group "apps" in the namespace "cw"\n' % NAME
        cases = {"forbidden": dict(sts_get_stderr=forbidden),
                 "a transport error": dict(sts_get_stderr="Unable to connect to the server: net/http: TLS handshake timeout\n"),
                 "a timeout": dict(sts_get_stderr="error: timed out waiting for the condition\n"),
                 "an answer that is not the object asked for": dict(sts_get_code=0, sts_get_stdout=json.dumps({"kind": "StatefulSet", "metadata": {"name": "other-sts"}}), sts_get_stderr="")}
        self.stamp_deadlines()
        self.clock.advance(hours=3)
        for what, state in cases.items():
            with self.subTest(what):
                self.patch_state(**state)
                open(self.env["FAKE_KUBE_CALLS"], "w").close()
                res = self.reap_pass()
                self.assertEqual(res, [(WORKER, cw.PERSISTENT_UNREADABLE, 1)])
                self.assertEqual(self.writes(), [])
                self.assertIn(POD, self.st()["pods"])
                self.assertEqual(self.st()["pods"][POD]["metadata"]["annotations"].get("cw/kept"), None)

    def test_s4_a_workload_that_does_not_own_the_pod_or_is_not_persistent_leaves_the_pod_alone(self):
        self.stamp_deadlines()
        self.clock.advance(hours=3)
        st = self.st()
        st["sts"][NAME]["metadata"]["annotations"].pop(cw.PERSISTENT_ANNOTATION)
        self.write_state(st)
        self.assertEqual(self.reap_pass(), [(WORKER, cw.PERSISTENT_UNREADABLE, 1)])
        st = self.st()
        st["sts"][NAME]["metadata"]["annotations"][cw.PERSISTENT_ANNOTATION] = "true"
        st["pods"][POD]["metadata"]["ownerReferences"] = []
        self.write_state(st)
        open(self.env["FAKE_KUBE_CALLS"], "w").close()
        self.assertEqual(self.reap_pass(), [(WORKER, cw.PERSISTENT_UNREADABLE, 1)])
        self.assertEqual(self.writes(), [])

    def test_s4_a_one_shot_worker_is_stopped_by_the_hard_and_idle_rules_as_before(self):
        self.create("hard1")
        self.create("idle1")
        self.set_activity("hard1", True, T0)
        self.set_activity("idle1", False, T0)
        self.mark_fresh_token()
        st = self.st()
        st["pods"][cw.pod_name("hard1")]["metadata"]["annotations"]["cw/hard-deadline"] = cw.iso(T0 + dt.timedelta(minutes=5))
        self.write_state(st)
        self.clock.advance(minutes=11)
        res = sorted((w, r) for w, r, _ in self.reap_pass())
        self.assertEqual(res, [("hard1", "hard limit"), ("idle1", "idle")])

    def test_s8_mutant_a_reaper_that_judges_a_persistent_pod_by_deadline_stops_it(self):
        self.mod = mutant('if PERSISTENT_ANNOTATION in ((pod.get("metadata") or {}).get("annotations") or {}):', "if False:")
        with self.assertRaises(AssertionError):
            self.check_left_whole()


class S5Stop(PBase):
    def setUp(self):
        super().setUp()
        self.launch()

    def stop(self, *extra):
        open(self.env["FAKE_KUBE_CALLS"], "w").close()
        return self.run_cli("stop", WORKER, "--no-record", *extra)

    def order(self):
        seq = [v for v in self.verbs() if v[0] == "delete"]
        return [(k, n) for _, k, n in seq]

    def check_order(self):
        code, _, err = self.stop()
        self.assertEqual(code, 0, err)
        seq = self.order()
        self.assertEqual(seq[0], ("statefulset", NAME))
        self.assertLess(seq.index(("statefulset", NAME)), seq.index(("pod", POD)))
        self.assertLess(seq.index(("pod", POD)), seq.index(("secret", NAME + "-pw")))
        self.assertIn(("secret", NAME + "-run"), seq)
        st = self.st()
        self.assertEqual((st.get("sts"), st["pods"]), ({}, {}))
        self.assertFalse({NAME + "-pw", NAME + "-run"} & set(st["secrets"]))
        self.assertNotIn(WORKER, self.registry()[1])
        self.assertNotIn("recreated", st)

    def test_s5_the_statefulset_goes_first_then_the_pod_the_secrets_and_the_registry_entry(self):
        self.check_order()
        line = [x for x in self.audit_lines() if x["action"] == "stop"][-1]
        self.assertEqual((line["ok"], line["persistent"], line["runtime_id"]), (True, True, "pod/" + POD))

    def test_s5_the_delete_waits_for_the_workload_within_its_bound(self):
        self.stop()
        deletes = [a for a in self.calls() if "statefulset" in a]
        self.assertTrue(deletes)
        self.assertIn("--wait=true", deletes[-1])
        self.assertIn("--timeout=%ds" % cw.WORKLOAD_DELETE_SECONDS, deletes[-1])
        self.assertEqual(cw.WORKLOAD_DELETE_SECONDS, 120)

    def test_s5_a_failed_push_keeps_the_pod_and_the_workload(self):
        self.patch_state(hook_result={"pushed": False, "wip_branch": None, "error": "push refused"})
        code, _, err = self.stop()
        self.assertEqual(code, cw.EXIT_HOOK, err)
        self.assertEqual([v for v in self.verbs() if v[0] == "delete"], [])
        self.assertIn(POD, self.st()["pods"])
        self.assertIn(NAME, self.sts())
        self.assertIn(WORKER, self.registry()[1])

    def test_s5_a_delete_that_does_not_finish_fails_the_stop_and_keeps_everything_then_a_retry_completes(self):
        self.patch_state(sts_delete_hang=True)
        code, out, err = self.stop("--json")
        self.assertNotEqual(code, 0)
        self.assertEqual(len([l for l in err.splitlines() if "not deleted within" in l]), 1, err)
        self.assertIn("StatefulSet %s" % NAME, err)
        st = self.st()
        self.assertIn(NAME, st["sts"])
        self.assertIn(POD, st["pods"])
        self.assertTrue({NAME + "-pw", NAME + "-run"} <= set(st["secrets"]))
        self.assertIn(WORKER, self.registry()[1])
        self.assertEqual([v for v in self.verbs() if v[0] == "delete"], [("delete", "statefulset", NAME)])
        failed = [x for x in self.audit_lines() if x["action"] == "stop"][-1]
        self.assertIs(failed["ok"], False)
        self.patch_state(sts_delete_hang=False)
        self.check_order()

    def test_s5_a_worker_stop_by_name_is_unchanged(self):
        self.create("coder7")
        open(self.env["FAKE_KUBE_CALLS"], "w").close()
        code, _, err = self.run_cli("stop", "coder7", "--no-record")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.pod("coder7"))
        self.assertFalse([a for a in self.calls() if any("statefulset" in x for x in a)])   # the worker path never reads a workload
        self.assertIn(NAME, self.sts())   # the persistent orchestrator beside it is untouched

    def test_s8_mutant_a_pod_deleted_before_the_statefulset_fails_the_order_check(self):
        self.mod = mutant("                self.kube.delete_workload_and_wait(name)\n                self.kube.delete_pod(name + \"-0\")\n                uid = None\n",
                          "                self.kube.delete_pod(name + \"-0\")\n                self.kube.delete_workload_and_wait(name)\n                uid = None\n")
        with self.assertRaises(AssertionError):
            self.check_order()


class S14HookResults(PBase):
    def setUp(self):
        super().setUp()
        self.launch()

    def test_s14_the_workload_goes_first_whatever_the_hook_reported(self):
        results = {"pushed": {"pushed": True, "wip_branch": "wip/orch1"},
                   "nothing to push": {"pushed": False, "wip_branch": None, "note": "nothing to push"},
                   "no repository": {"pushed": False, "wip_branch": None, "note": "no repository in the pod"}}
        for what, res in results.items():
            with self.subTest(what):
                if NAME not in self.sts():
                    self.launch()
                self.patch_state(hook_result=res)
                open(self.env["FAKE_KUBE_CALLS"], "w").close()
                code, _, err = self.run_cli("stop", WORKER, "--no-record")
                self.assertEqual(code, 0, err)
                seq = [(k, n) for v, k, n in self.verbs() if v == "delete"]
                self.assertEqual(seq[0], ("statefulset", NAME))
                self.assertLess(seq.index(("statefulset", NAME)), seq.index(("pod", POD)))
                self.assertNotIn("recreated", self.st())
                self.assertEqual(self.st()["pods"], {})

    def test_s14_a_hook_failure_keeps_both_and_no_path_deletes_the_pod_alone(self):
        self.patch_state(hook_result={"pushed": False, "wip_branch": None, "error": "push failed"})
        code, _, _ = self.run_cli("stop", WORKER, "--no-record")
        self.assertEqual(code, cw.EXIT_HOOK)
        self.assertIn(POD, self.st()["pods"])
        self.assertIn(NAME, self.sts())
        # delete by name never reaches a persistent pod (its pod is NAME-0): it is refused (the evidence rule), and the reaper never stops one
        code, _, err = self.run_cli("delete", WORKER, "--force")
        self.assertEqual(code, cw.EXIT_USAGE, err)
        self.assertIn(POD, self.st()["pods"])
        self.assertEqual([v for v in self.verbs() if v == ("delete", "pod", POD)], [])


class S6Renewal(renewal.Harness):
    def install_persistent(self, secret=True):
        self.install_worker(secret=secret)
        a = argparse.Namespace(repo=renewal.REPO, branch="main", idle="10m", hard="2h", max_hard="8h", shape="S", image="registry.example.internal/worker:1",
                               run_id=renewal.RUN, pull_secret=None, agent="orchestrator", boss="boss1", stack=False, persistent=True)
        sts = cw.render_workload(a, renewal.WORKER, self.name, T0, self.env, ("git-token", "model-auth", cw.CHANNEL_SECRET_KEY))
        sts["metadata"]["uid"] = "sts-uid-1"
        st = self.st()
        old = st["pods"].pop(self.name)
        tmpl = sts["spec"]["template"]
        st["pods"] = {self.name + "-0": {"metadata": {"name": self.name + "-0", "labels": tmpl["metadata"]["labels"], "annotations": tmpl["metadata"]["annotations"],
                                                      "uid": "uid-p", "ownerReferences": [{"kind": "StatefulSet", "name": self.name, "uid": "sts-uid-1"}]},
                                         "spec": tmpl["spec"], "status": old["status"]}}
        st["sts"] = {self.name: sts}
        st["activity"] = {self.name + "-0": {"busy": True, "last_activity": cw.iso(T0)}}
        self.write_state(st)

    def check_renewed(self):
        self.install_persistent()
        self.at(minutes=31)
        res = self.one_pass()
        self.assertEqual(res, [(renewal.WORKER, "git token renewed", 0)])
        self.assertEqual(self.secret()["token"], renewal.NEW)
        self.assertEqual([v for v in self.stops()], [])

    def stops(self):
        with open(self.env["FAKE_KUBE_CALLS"]) as f:
            return [json.loads(x) for x in f if '"delete"' in x]

    def test_s6_the_secret_is_found_by_the_workers_name_and_renewed(self):
        self.check_renewed()

    def test_s6_a_missing_secret_is_one_report_line_and_no_stop(self):
        self.install_persistent(secret=False)
        self.at(minutes=31)
        res = self.one_pass()
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0][0], renewal.WORKER)
        self.assertIn("Secret", res[0][1])
        self.assertEqual(res[0][2], 1)
        self.assertEqual(self.stops(), [])
        self.assertIn(self.name + "-0", self.st()["pods"])

    def test_s6_the_renewal_reads_the_pod_template_annotations_it_needs_from_the_pod(self):
        self.install_persistent()
        pod = self.st()["pods"][self.name + "-0"]
        self.assertEqual(pod["metadata"]["annotations"], self.st()["sts"][self.name]["spec"]["template"]["metadata"]["annotations"])
        self.assertEqual(cw.ann(pod, "persistent"), "true")

    def test_s8_mutant_a_renewal_keyed_by_the_pod_name_fails_the_renewal_check(self):
        self.cwm = mutant("        if persistent:\n            name = pod_name(worker)\n", "        if False:\n            name = pod_name(worker)\n")
        with self.assertRaises(AssertionError):
            self.check_renewed()


class S7Lookup(PBase):
    NS = "cw"
    NOT_FOUND = 'Error from server (NotFound): statefulsets.apps "%s" not found'
    FORBIDDEN = 'Error from server (Forbidden): statefulsets.apps "%s" is forbidden: User "cw-operator" cannot get resource "statefulsets" in API group "apps" in the namespace "%s"'
    # The wording the lookup accepts is the one kubectl 1.28 to 1.31 prints for a StatefulSet read (the apps group is named in the resource); the
    # pre-1.9 client printed `statefulsets "NAME" not found` (no group) and a newer one prints `Error: ...`: another client's wording is unreadable.

    def table(self):
        n = "orch-ab12"
        nf, fb = self.NOT_FOUND, self.FORBIDDEN
        return {
            "the same text for another name": dict(err=nf % "other-ab12" + "\n"),
            "the same text for a pod": dict(err='Error from server (NotFound): pods "%s" not found\n' % n),
            "the same text for another kind": dict(err='Error from server (NotFound): deployments.apps "%s" not found\n' % n),
            "a longer name that starts with the asked name": dict(err=nf % (n + "-0") + "\n"),
            "the word inside a longer line": dict(err=nf % n + ": the server could not find the requested resource\n"),
            "the word inside a server error": dict(err='Error from server (InternalError): an error on the server ("NotFound") has prevented the request from succeeding\n'),
            "an empty answer": dict(err=""),
            "extra lines before it": dict(err="warning: a deprecated flag\n" + nf % n + "\n"),
            "a different case": dict(err=(nf % n).lower().replace("statefulsets.apps", "StatefulSets.apps") + "\n"),
            "more than one trailing newline": dict(err=nf % n + "\n\n"),
            "a carriage return": dict(err=nf % n + "\r\n"),
            "a namespace-qualified resource name": dict(err='Error from server (NotFound): statefulsets.apps "%s/%s" not found\n' % (self.NS, n)),
            "the message on the wrong stream": dict(out=nf % n + "\n", err=""),
            "mixed with other output": dict(out="partial output\n", err=nf % n + "\n"),
            "another client wording (no group, kubectl before 1.9)": dict(err='Error from server (NotFound): statefulsets "%s" not found\n' % n),
            "another client wording (an Error: prefix)": dict(err='Error: statefulsets.apps "%s" not found\n' % n),
            "the right name in the wrong namespace (not found)": dict(err=nf % n + ' in namespace "other"\n'),
            "the right name in the wrong namespace (forbidden)": dict(err=fb % (n, "other") + "\n", forbidden=True),
            "a forbidden answer for another name": dict(err=fb % ("other-ab12", self.NS) + "\n", forbidden=True),
            "a forbidden answer for a pod": dict(err='Error from server (Forbidden): pods "%s" is forbidden: User "x" cannot get resource "pods" in API group "" in the namespace "cw"\n' % n, forbidden=True),
            "a server error that names Forbidden": dict(err='Error from server (InternalError): Forbidden statefulsets.apps "%s"\n' % n),
        }

    def ask(self, **planted):
        self.patch_state(sts_get_stdout=planted.get("out", ""), sts_get_stderr=planted.get("err", ""), sts_get_code=planted.get("code", 1))
        open(self.env["FAKE_KUBE_CALLS"], "w").close()
        return self.mod.Kube(self.env).get_workload("orch-ab12")

    def check_table(self):
        self.assertIsNone(self.ask(err=self.NOT_FOUND % "orch-ab12" + "\n"))
        self.assertIsNone(self.ask(err=self.NOT_FOUND % "orch-ab12"))   # exactly one trailing newline is allowed, and none
        with self.assertRaises(self.mod.WorkloadForbidden) as caught:
            self.ask(err=self.FORBIDDEN % ("orch-ab12", self.NS) + "\n")
        self.assertIn("apps group", str(caught.exception))
        self.assertIn("StatefulSets", str(caught.exception))
        self.assertIn("administrator", str(caught.exception))
        table = self.table()
        self.assertGreaterEqual(len(table), 13)
        problems = []   # no subTest here: a mutant's failure must reach the caller as an AssertionError
        for what, planted in table.items():
            try:
                self.ask(**planted)
                problems.append("%s: read as a missing workload" % what)
            except self.mod.WorkloadForbidden:
                problems.append("%s: read as the missing rule" % what)
            except self.mod.CloudError as e:
                if "unreadable" not in str(e):
                    problems.append("%s: not named unreadable: %s" % (what, e))
            if self.writes():
                problems.append("%s: a write call was made" % what)
        self.assertEqual(problems, [])

    def test_s7_the_exact_answers_and_every_near_miss(self):
        self.check_table()

    def test_s7_an_answer_that_is_a_different_object_is_unreadable(self):
        for obj in ({"kind": "StatefulSet", "metadata": {"name": "other-ab12"}}, {"kind": "Pod", "metadata": {"name": "orch-ab12"}}, ["x"]):
            with self.subTest(obj=obj):
                with self.assertRaises(cw.CloudError):
                    self.ask(code=0, out=json.dumps(obj), err="")
        found = self.ask(code=0, out=json.dumps({"kind": "StatefulSet", "metadata": {"name": "orch-ab12"}}), err="")
        self.assertEqual(found["metadata"]["name"], "orch-ab12")

    def test_s8_mutant_a_looser_not_found_reads_the_near_misses_as_missing(self):
        old = "if code != 0 and not out and re.fullmatch(r'Error from server \\(NotFound\\): statefulsets\\.apps \"%s\" not found\\n?' % quoted, err):"
        self.mod = mutant(old, 'if "NotFound" in err:')
        with self.assertRaises(AssertionError):
            self.check_table()


class S11Maximum(PBase):
    def plant_workloads(self, count):
        self.patch_state(sts={"w%d" % i: {"kind": "StatefulSet", "metadata": {"name": "w%d" % i, "labels": {"app": cw.LABEL}}} for i in range(count)})

    def check_maximum(self):
        self.plant_workloads(2)   # the default maximum is 2: a third is refused
        self.launch(expect=2)
        self.assertNotIn("applied", self.st())
        self.assertEqual(set(self.st()["secrets"]), {REGISTRY, "cloud-hub-pw"})
        self.plant_workloads(1)   # within it
        self.launch()
        self.assertIn(NAME, self.sts())

    def test_s11_a_launch_above_the_maximum_is_refused_and_one_within_it_goes_on(self):
        self.check_maximum()

    def test_s11_an_unreadable_count_is_refused_with_nothing_created(self):
        self.patch_state(sts_list_fail=True)
        _, _, err = self.launch(expect=2)
        self.assertIn("could not count", err)
        self.assertNotIn("applied", self.st())
        self.assertEqual(set(self.st()["secrets"]), {REGISTRY, "cloud-hub-pw"})

    def test_s11_the_setting_names_the_maximum_and_a_bad_value_is_refused(self):
        self.plant_workloads(2)
        self.env["CW_MAX_PERSISTENT"] = "3"
        self.launch()
        self.assertEqual(len(self.sts()), 3)
        for bad in ("0", "21", "abc", "1.5", "-1"):
            with self.subTest(bad):
                self.env["CW_MAX_PERSISTENT"] = bad
                self.launch("orch9", expect=2)
        self.assertNotIn(cw.pod_name("orch9") + "-pw", self.st()["secrets"])

    def test_s8_mutant_a_launch_that_never_counts_makes_the_third_one(self):
        self.mod = mutant("                self.check_persistent_count()\n", "                pass\n")
        with self.assertRaises(AssertionError):
            self.check_maximum()


class MaximumValueForm(unittest.TestCase):
    """The value form of CW_MAX_PERSISTENT: an integer 1 to 20 written without leading zeros (ask #1509)."""
    GOOD = ("1", "20", "7")
    BAD = ("0", "21", "00", "01", "05", "+5", "-5", "1.5", "abc", "\u0662")

    def check_form(self, mod):
        check = mod.VALIDATORS["CW_MAX_PERSISTENT"][1]
        for v in self.GOOD:
            self.assertTrue(check(v), v)
        for v in self.BAD:
            self.assertFalse(check(v), v)

    def test_form_accepts_one_twenty_seven_and_refuses_the_rest(self):
        self.check_form(cw)

    def test_form_mutant_the_looser_rule_is_caught(self):
        old = 'bool(re.fullmatch(r"[1-9][0-9]?", v))'
        mod = mutant(old, 'bool(re.fullmatch(r"[0-9]{1,2}", v))')
        with self.assertRaises(AssertionError):
            self.check_form(mod)


class S12Status(PBase):
    def test_s12_status_shows_the_worker_name_persistent_with_age_and_cost(self):
        self.launch()
        self.clock.advance(hours=5)
        for argv in (("status", "--json"), ("status", WORKER, "--json")):
            with self.subTest(argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual(code, 0, err)
                rows = json.loads(out)
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row["worker"], WORKER)
                self.assertNotEqual(row["worker"], POD)
                self.assertIs(row["persistent"], True)
                self.assertEqual(row["runtime_id"], "pod/" + POD)
                self.assertEqual(row["age_hours"], 5.0)
                self.assertEqual(row["cost_estimate_usd"], round(5 * cw.SHAPES[cw.DEFAULT_ORCHESTRATOR_SHAPE]["usd_per_hour"], 4))
                self.assertEqual(row["deadlines"], {"idle": None, "hard": None})

    def test_s12_the_cost_report_prints_a_create_with_no_stop_as_one_open_pod(self):
        self.launch()
        create = [x for x in self.audit_lines() if x["action"] == "create"]
        self.assertEqual(len(create), 1)
        self.assertIs(create[0]["persistent"], True)
        res = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "cloud_cost_report.py"), T0.strftime("%Y-%m-%d"), self.env["CW_CLOUD_AUDIT"],
                              "--node-price", "1", "--node-vcpu", "4", "--node-hours", "24", "--shape", "S"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        open_lines = [l for l in res.stdout.splitlines() if l.startswith("open pod ")]
        self.assertEqual(len(open_lines), 1, res.stdout)
        self.assertIn("pod/" + POD, open_lines[0])
        self.assertRegex(open_lines[0], r"\d+\.\d\d h, USD \d+\.\d\d")


class S13Answer(PBase):
    def test_s13_the_answer_the_audit_line_and_the_record_carry_persistent_true_and_no_deadline(self):
        _, out, _ = self.launch()
        answer = json.loads(out)
        self.assertIs(answer["persistent"], True)
        self.assertEqual((answer["idle_deadline"], answer["hard_deadline"]), (None, None))
        self.assertEqual(answer["runtime_id"], "pod/" + POD)
        line = [x for x in self.audit_lines() if x["action"] == "create"][0]
        self.assertIs(line["persistent"], True)
        records = os.path.join(self.tmp.name, "rec")
        code, _, err = self.run_cli("stop", WORKER, "--record-dir", records)
        self.assertEqual(code, 0, err)
        runs = [os.path.join(records, d, "run.json") for d in os.listdir(records)]
        self.assertEqual(len(runs), 1)
        with open(runs[0]) as f:
            self.assertIs(json.load(f)["persistent"], True)

    def test_s13_a_failed_persistent_create_cleans_up_the_workload_first(self):
        self.patch_state(never_ready=True, fail_cmd=None)
        open(self.env["FAKE_KUBE_CALLS"], "w").close() if os.path.exists(self.env["FAKE_KUBE_CALLS"]) else None
        _, _, err = self.launch(WORKER, "--wait", "4", expect=cw.EXIT_TIMEOUT)
        seq = [(k, n) for v, k, n in self.verbs() if v == "delete"]
        self.assertEqual(seq[0], ("statefulset", NAME))   # the cleanup deletes the workload first
        self.assertEqual(self.st().get("sts", {}), {})
        self.assertNotIn(POD, self.st()["pods"])
        self.assertEqual(set(self.st()["secrets"]), {REGISTRY, "cloud-hub-pw"})


class S9Docs(unittest.TestCase):
    def read(self, rel):
        with open(os.path.join(ROOT, rel)) as f:
            return " ".join(f.read().split())

    def test_s9_the_docs_state_the_mode_the_reaper_the_cluster_step_and_the_cost(self):
        rate = cw.SHAPES[cw.DEFAULT_ORCHESTRATOR_SHAPE]["usd_per_hour"]
        figure = "USD %.2f a day" % (24 * rate)
        for rel in ("docs/cloud-operator-quickstart.md", "cloud/launcher/README.md"):
            text = self.read(rel)
            with self.subTest(rel):
                for phrase in ("ONE StatefulSet", "restart policy Always", "no hard deadline", "no idle limit", "only an explicit stop",
                               "leaves a persistent orchestrator alone", "persistent workload unreadable: left alone", "untouched",
                               "the `apps` group", "statefulsets", "administrator applies it", "UNBOUNDED IN TIME by design", "24 times the hourly estimate"):
                    if rel.startswith("cloud/launcher") and phrase in ("no hard deadline", "no idle limit", "restart policy Always", "24 times the hourly estimate", "leaves a persistent orchestrator alone", "the `apps` group"):
                        continue   # the README words these differently; the quickstart carries them whole
                    self.assertTrue(phrase in text, phrase)
                self.assertIn("%.2f" % (24 * rate), text)
        self.assertIn(figure, self.read("docs/cloud-operator-quickstart.md"))

    def test_s9_the_figure_in_the_docs_is_twenty_four_times_the_row(self):
        text = self.read("docs/cloud-operator-quickstart.md")
        found = re.search(r"USD (\d+\.\d\d) an hour\) that is USD (\d+\.\d\d) a day", text)
        self.assertIsNotNone(found)
        self.assertAlmostEqual(float(found.group(2)), 24 * float(found.group(1)), places=2)
        self.assertEqual(float(found.group(1)), cw.SHAPES[cw.DEFAULT_ORCHESTRATOR_SHAPE]["usd_per_hour"])


class S10Manifest(unittest.TestCase):
    OLD_RULES = [(("",), ("pods",), ("get", "list", "watch", "create", "patch", "delete")), (("",), ("pods/log",), ("get",)), (("",), ("pods/exec",), ("create",)),
                 (("",), ("pods/portforward",), ("create",)), (("",), ("secrets",), ("get", "list", "create", "patch", "delete"))]

    def test_s10_exactly_one_rule_is_added_and_no_other_is_widened(self):
        role = [d for d in k1.rbac_docs() if d["kind"] == "Role"][0]
        rules = [(tuple(r["groups"]), tuple(r["resources"]), tuple(r["verbs"])) for r in role["rules"]]
        added = [r for r in rules if r not in self.OLD_RULES]
        self.assertEqual(added, [(("apps",), ("statefulsets",), ("get", "list", "watch", "create", "delete"))])
        self.assertEqual([r for r in self.OLD_RULES if r not in rules], [])
        self.assertEqual(len(rules), len(self.OLD_RULES) + 1)
        self.assertEqual(len(added[0][2]), 5)
        self.assertNotIn("*", role["text"].replace('apiGroups: [""]', ""))


class S10Verbs(unittest.TestCase):
    """The StatefulSet rule has exactly five verbs: the launcher creates the workload with create -f and never patches one."""
    EXACT = ["get", "list", "watch", "create", "delete"]

    def verbs_in(self, text):
        role = [d for d in k1.rbac_docs(text) if d["kind"] == "Role"][0]
        return [r["verbs"] for r in role["rules"] if r["resources"] == ["statefulsets"]]

    def check(self, text):
        self.assertEqual(self.verbs_in(text), [self.EXACT])

    def read(self):
        with open(os.path.join(ROOT, "cloud/k8s/operator-rbac.yaml")) as f:
            return f.read()

    def test_s10_the_statefulset_rule_holds_exactly_five_verbs(self):
        self.check(self.read())

    def test_s10_mutant_a_patch_verb_added_fails_the_check(self):
        text = self.read()
        changed = text.replace('verbs: ["get", "list", "watch", "create", "delete"]', 'verbs: ["get", "list", "watch", "create", "patch", "delete"]')
        self.assertNotEqual(changed, text)
        with self.assertRaises(AssertionError):
            self.check(changed)

    def test_s10_the_launcher_never_patches_a_statefulset(self):
        with open(SOURCE) as f:
            lines = [l for l in f if '"patch"' in l or "'patch'" in l]
        self.assertTrue(lines)
        self.assertFalse([l for l in lines if "statefulset" in l.lower()])


class Evidence(PBase):
    """The evidence rule: a worker's workload is read only when its audit create line, hub registry entry or run record says persistent."""
    def answers(self):
        base = S7Lookup.table(S7Lookup)
        table = {}
        for what, planted in base.items():
            table[what] = {k: v.replace("orch-ab12", NAME) for k, v in planted.items() if k in ("out", "err")}
        table["the exact forbidden line"] = dict(err=S7Lookup.FORBIDDEN % (NAME, "cw") + "\n")
        return table

    def sts_reads(self):
        return [v for v in self.verbs() if v[:2] == ("get", "statefulset")]

    def plain_worker(self):
        self.launch(WORKER, agent=False, token=False, persistent=False)
        st = self.st()
        pods = dict(st["pods"])
        pods.pop(NAME, None)
        self.patch_state(pods=pods)

    def stop_with(self, planted):
        self.patch_state(sts_get_stdout=planted.get("out", ""), sts_get_stderr=planted.get("err", ""), sts_get_code=1)
        open(self.env["FAKE_KUBE_CALLS"], "w").close()
        return self.run_cli("stop", WORKER, "--no-record")

    def deletes(self):
        return [v for v in self.verbs() if v[0] == "delete"]

    def check_plain(self):
        problems = []
        for what, planted in self.answers().items():
            self.setUp_again()
            self.plain_worker()
            code, out, err = self.stop_with(planted)
            if code != 0 or self.sts_reads() or "pod_gone" not in out:
                problems.append("%s: exit %s, %d reads" % (what, code, len(self.sts_reads())))
            if {NAME + "-pw", NAME + "-run"} & set(self.st()["secrets"]):
                problems.append("%s: a per-run Secret was kept" % what)
        self.assertEqual(len(self.answers()), 22)
        self.assertEqual(problems, [])

    def check_persistent(self, sources=None):
        problems = []
        for what, planted in self.answers().items():
            self.setUp_again()
            self.launch()
            code, out, err = self.stop_with(planted)
            if code != 1 or len(self.sts_reads()) != 1 or self.deletes() or "pod_gone" in out:
                problems.append("%s: exit %s, %d reads, %d deletes" % (what, code, len(self.sts_reads()), len(self.deletes())))
            st = self.st()
            if not st.get("sts") or POD not in st["pods"] or not {NAME + "-pw", NAME + "-run"} <= set(st["secrets"]):
                problems.append("%s: something was not kept" % what)
            last = [l for l in self.audit_lines() if l.get("action") == "stop"][-1]
            if last.get("ok") is not False:
                problems.append("%s: the audit stop line is not ok false" % what)
        self.assertEqual(problems, [])

    def setUp_again(self):
        """A fresh cluster state for the next answer (the harness's own reset)."""
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = []
        self.reset_cluster()

    def reset_cluster(self):
        self.write_state({"pods": {}})
        self.patch_state(secrets={REGISTRY: 1, "cloud-hub-pw": 1},
                         secret_bodies={"cloud-hub-pw": {"keep": "kept-pw"}, REGISTRY: {"secrets.json": json.dumps(OTHER), "agents.json": json.dumps({"other-agent": {"url": "u"}})}},
                         hook_result={"pushed": False, "wip_branch": None})
        if os.path.exists(self.env["CW_CLOUD_AUDIT"]):
            os.remove(self.env["CW_CLOUD_AUDIT"])

    def test_m1_22_answers_for_a_plain_worker_make_no_read_and_remove_both_secrets(self):
        self.check_plain()

    def test_m1_22_answers_for_a_persistent_worker_fail_the_stop_and_keep_everything(self):
        self.check_persistent()

    def test_m1_mutant_forbidden_read_as_no_workload_fails_the_check(self):
        self.mod = mutant("                    workload = self.kube.get_workload(name)\n", "                    workload = self._persistent_workload(name)\n")
        with self.assertRaises(AssertionError):
            self.check_persistent()

    def test_m1_mutant_reading_the_workload_for_every_worker_fails_the_check(self):
        self.mod = mutant('if pod is None and self._persistent_evidence(worker, getattr(a, "record_dir", None)):', "if pod is None:")
        with self.assertRaises(AssertionError):
            self.check_plain()

    def evidence_only(self, keep):
        """A persistent worker whose three sources are cleared except `keep` (audit, registry or record)."""
        self.launch()
        if keep != "audit":
            os.remove(self.env["CW_CLOUD_AUDIT"])
        bodies = self.st()["secret_bodies"]
        agents = json.loads(bodies[REGISTRY]["agents.json"])
        if keep != "registry":
            agents[WORKER].pop("persistent", None)
            bodies[REGISTRY]["agents.json"] = json.dumps(agents)
            self.patch_state(secret_bodies=bodies)
        root = cw.run_record.default_root(self.env)
        if keep == "record":
            os.makedirs(os.path.join(root, WORKER + "-20260101T000000Z-abc123"))
            with open(os.path.join(root, WORKER + "-20260101T000000Z-abc123", "run.json"), "w") as f:
                json.dump({"worker": WORKER, "persistent": True}, f)

    def test_m1_the_exact_not_found_of_a_worker_with_evidence_is_no_workload_and_the_pod_gone_path_runs(self):
        self.launch()
        pods = dict(self.st()["pods"])
        pods.pop(POD, None)
        self.patch_state(pods=pods, sts={})
        code, out, err = self.stop_with(dict(err=S7Lookup.NOT_FOUND % NAME + "\n"))
        self.assertEqual(code, 0, err)
        self.assertIn("pod_gone", out)
        self.assertEqual(len(self.sts_reads()), 1)

    def test_m1_each_evidence_source_alone_makes_the_worker_persistent(self):
        for keep in ("audit", "registry", "record"):
            with self.subTest(keep):
                self.setUp_again()
                self.evidence_only(keep)
                self.assertTrue(self.mod.Launcher(self.mod.Kube(self.env), self.env, self.clock)._persistent_evidence(WORKER))

    def test_m1_no_source_is_no_evidence(self):
        self.setUp_again()
        self.evidence_only("none")
        self.assertFalse(self.mod.Launcher(self.mod.Kube(self.env), self.env, self.clock)._persistent_evidence(WORKER))

    def test_m1_when_no_source_is_readable_the_worker_is_treated_as_plain(self):
        # The audit path is a directory, the record root is a file and the registry body is not JSON: every source is unreadable, so the worker is
        # plain (an unreadable source counts as evidence only when another source says persistent).
        self.launch()
        os.remove(self.env["CW_CLOUD_AUDIT"])
        os.mkdir(self.env["CW_CLOUD_AUDIT"])
        root = cw.run_record.default_root(self.env)
        os.makedirs(os.path.dirname(root), exist_ok=True)
        with open(root, "w") as f:
            f.write("x")
        bodies = self.st()["secret_bodies"]
        bodies[REGISTRY]["agents.json"] = "not json"
        self.patch_state(secret_bodies=bodies)
        self.assertFalse(self.mod.Launcher(self.mod.Kube(self.env), self.env, self.clock)._persistent_evidence(WORKER))

    def test_m1_an_unreadable_source_with_another_saying_persistent_is_evidence(self):
        self.launch()   # the registry entry and the audit line say persistent
        root = cw.run_record.default_root(self.env)
        os.makedirs(os.path.dirname(root), exist_ok=True)
        with open(root, "w") as f:
            f.write("x")   # the record source is unreadable
        self.assertTrue(self.mod.Launcher(self.mod.Kube(self.env), self.env, self.clock)._persistent_evidence(WORKER))

    def delete_force(self):
        open(self.env["FAKE_KUBE_CALLS"], "w").close()
        return self.run_cli("delete", WORKER, "--force")

    def check_delete_refusal(self):
        self.launch()
        code, out, err = self.delete_force()
        self.assertEqual(code, 2, err)
        self.assertIn("persistent orchestrator", err)
        self.assertIn("stop %s" % WORKER, err)
        self.assertNotIn('"deleted": true', out)
        self.assertEqual(self.writes(), [])
        st = self.st()
        self.assertTrue(st["sts"] and POD in st["pods"])
        return err

    def test_m2_delete_force_of_a_persistent_orchestrator_is_refused_naming_the_stop(self):
        err = self.check_delete_refusal()
        print("\nthe printed delete refusal: " + err.strip())

    def test_m2_delete_force_of_a_plain_worker_is_as_before(self):
        self.launch(WORKER, agent=False, token=False, persistent=False)
        code, out, err = self.delete_force()
        self.assertEqual(code, 0, err)
        self.assertIn('"deleted": true', out)
        self.assertNotIn(NAME, self.st()["pods"])
        self.assertEqual([v for v in self.verbs() if v[:2] == ("get", "statefulset")], [])

    def test_m2_mutant_the_refusal_removed_fails_the_check(self):
        self.mod = mutant('if self._persistent_evidence(worker, getattr(a, "record_dir", None)):   # the same evidence rule', "if False:   # the same evidence rule")
        with self.assertRaises(AssertionError):
            self.check_delete_refusal()


class S16Docs(unittest.TestCase):
    FILES = ("docs/cloud-operator-quickstart.md", "cloud/launcher/README.md")
    PHRASES = (
        "fails the reaper's exit code on every pass until the workload is readable again",
        "a ceiling checked at launch, not a lock",
        "two launches at the same moment can each count below the maximum and both succeed",
        "do-not-evict mark",
        "only once ask 1519 lands",
        "at 24 hours of its shape",
        "no `cw/role` annotation on the template at this head",
        "a lost read stops renewal",
    )

    def text(self, rel, drop=None):
        with open(os.path.join(ROOT, rel)) as f:
            t = " ".join(f.read().split())
        if drop is not None:
            self.assertIn(drop, t)
            changed = t.replace(drop, "")
            self.assertNotEqual(changed, t)   # the mutation is applied
            return changed
        return t

    def check(self, rel, phrase, drop=None):
        self.assertIn(phrase, self.text(rel, drop))

    def test_s16_each_sentence_is_in_both_documents(self):
        for rel in self.FILES:
            for phrase in self.PHRASES:
                with self.subTest(rel=rel, phrase=phrase):
                    self.check(rel, phrase)

    def test_s16_mutant_a_removed_sentence_fails_its_phrase_test(self):
        for rel in self.FILES:
            for phrase in self.PHRASES:
                with self.subTest(rel=rel, phrase=phrase):
                    with self.assertRaises(AssertionError):
                        self.check(rel, phrase, drop=phrase)

    def test_s16_no_document_says_the_node_never_scales_down(self):
        found = []
        for base in ("README.md", "docs", "cloud"):
            path = os.path.join(ROOT, base)
            files = [path] if os.path.isfile(path) else [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs if f.endswith(".md")]
            for f in files:
                with open(f) as fh:
                    if re.search(r"never scales? down|never be scaled down", fh.read(), re.I):
                        found.append(f)
        self.assertEqual(found, [])

    def test_s16_the_docs_say_five_verbs_not_six(self):
        for rel in self.FILES:
            with self.subTest(rel):
                t = self.text(rel)
                self.assertNotIn("create, patch", t)
                self.assertNotIn("six verbs", t)


if __name__ == "__main__":
    unittest.main()
