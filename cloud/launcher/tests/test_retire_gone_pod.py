"""A retire of a worker whose pod is already gone frees its name (ask #1125, scenarios S7-S10, U3).

The launcher side runs against the fake kubectl; the retire side runs cmd_retire with the launcher call replaced by a stub reply.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as tcw  # noqa: E402

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "bin")
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("bin_cloud_worker", os.path.join(BIN, "cloud_worker.py"))
sbcw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sbcw)


def plant_run_secret(case):
    """A create without run secrets leaves only the password Secret: add the run Secret a launch with run secrets makes."""
    st = case.st()
    st.setdefault("secrets", {})[tcw.cw.pod_name("coder7") + "-run"] = 1
    case.write_state(st)


class Launcher(tcw.Base):
    def test_gone_pod_stop_says_so_and_touches_nothing_else(self):
        self.create("coder7")
        plant_run_secret(self)
        st = self.st()
        st["pods"].pop(tcw.cw.pod_name("coder7"))   # an operator deleted the pod; both Secrets stay
        self.write_state(st)
        self.assertTrue(self.st()["secrets"])
        code, out, err = self.run_cli("stop", "coder7", "--reason", "retire")
        self.assertEqual(code, 0, err)
        self.assertIs(json.loads(out).get("pod_gone"), True)
        self.assertEqual(json.loads(out)["secrets_removed"], [tcw.cw.pod_name("coder7") + "-pw", tcw.cw.pod_name("coder7") + "-run"])
        self.assertEqual(self.st()["secrets"], {})

    def test_live_pod_stop_never_says_gone(self):
        self.create("coder7")
        with mock.patch.object(tcw.cw.Launcher, "run_hook", lambda self, name: {"pushed": False, "wip_branch": None}):
            code, out, err = self.run_cli("stop", "coder7", "--reason", "retire")
        self.assertEqual(code, 0, err)
        self.assertNotIn("pod_gone", json.loads(out))

    def test_unreadable_pod_read_is_not_gone(self):
        self.create("coder7")
        self.patch_state(fail_cmd="get")
        code, out, err = self.run_cli("stop", "coder7", "--reason", "retire")
        self.assertNotEqual(code, 0)
        self.assertNotIn("pod_gone", out)
        self.assertIsNotNone(self.pod())


def _hook_fails(self, name):
    return {"pushed": False, "wip_branch": None, "error": "no remote"}


class SecretsOfLivePods(tcw.Base):
    """S15: the new rule never removes a Secret unless the pod read said NotFound."""
    def pw(self):
        return tcw.cw.pod_name("coder7")

    def stop_leaves_secrets(self):
        """True when after a stop of the case set up by the caller both Secrets are still there."""
        with mock.patch.object(tcw.cw.Launcher, "run_hook", _hook_fails):
            code, out, err = self.run_cli("stop", "coder7", "--reason", "retire")
        self.last = (code, out)
        return sorted(self.st()["secrets"]) == [self.pw() + "-pw", self.pw() + "-run"]

    def cases(self):
        """Yield a label after setting up each live or unreadable case on a fresh created worker."""
        self.create("coder7")
        plant_run_secret(self)
        yield "running"
        st = self.st()
        st["pods"][self.pw()]["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
        self.write_state(st)
        yield "terminating"
        st = self.st()
        st["pods"][self.pw()]["metadata"].pop("deletionTimestamp")
        st["pods"][self.pw()]["metadata"]["uid"] = "recreated-uid"
        st["pods"][self.pw()]["metadata"]["annotations"] = {"cw/run-id": "another-run"}
        self.write_state(st)
        yield "recreated"
        self.patch_state(fail_cmd="get")
        yield "kubectl error"
        self.patch_state(fail_cmd=None)
        real = tcw.cw.Kube.run

        def timeout(kube, args, *a, **k):
            if args[:2] == ["get", "pod"]:
                raise tcw.cw.CloudError("kubectl failed: timed out")
            return real(kube, args, *a, **k)

        def malformed(kube, args, *a, **k):
            return "{not json" if args[:2] == ["get", "pod"] else real(kube, args, *a, **k)
        with mock.patch.object(tcw.cw.Kube, "run", timeout):
            yield "timeout"
        with mock.patch.object(tcw.cw.Kube, "run", malformed):
            yield "malformed"

    def removed_in(self):
        return [label for label in self.cases() if not self.stop_leaves_secrets()]

    def test_s15_no_secret_removed_for_live_or_unreadable_pods(self):
        self.assertEqual(self.removed_in(), [])

    def test_s15_mutant_removing_secrets_whenever_stop_runs_is_caught(self):
        real, applied = tcw.cw.Launcher.stop, []

        def mutant(launcher, a, reason=None):
            applied.append(1)
            launcher.kube.delete_secret("coder7-" + "x")   # no-op name: keep the mutation obvious but harmless
            launcher.kube.delete_secret(launcher_name(a))
            launcher.kube.delete_run_secret(launcher_name(a))
            return real(launcher, a, reason)

        def launcher_name(a):
            return tcw.cw.pod_name(a.worker)
        with mock.patch.object(tcw.cw.Launcher, "stop", mutant):
            caught = self.removed_in()
        self.assertTrue(applied, "the mutation was not applied")
        self.assertTrue(caught, "the mutant was not killed")


class EndToEnd(tcw.Base):
    """S14: a retire of a gone pod with both Secrets left, then a launch of the same name."""
    def test_s14_retire_removes_both_secrets_audits_two_and_the_name_launches_again(self):
        self.create("coder7")
        plant_run_secret(self)
        st = self.st()
        st["pods"].pop(tcw.cw.pod_name("coder7"))
        st["secrets"]["other-worker-pw"] = 1   # another name's Secret stays
        self.write_state(st)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = os.path.join(tmp.name, "state")
        os.makedirs(os.path.join(state, "cloud"))
        record = os.path.join(state, "cloud", "coder7.json")
        with open(record, "w") as handle:
            handle.write("{}")
        audit = []
        engine = types.SimpleNamespace(actor="op", audit=types.SimpleNamespace(append=lambda actor, ev, name, d: audit.append((ev, name, d))))
        over = {"LaunchError": RuntimeError, "build_engine": lambda r, n, c: (engine, state, None),
                "gates_caller_kind": lambda r, n: ("person", "x"), "EXIT_OK": 0, "EXIT_ERROR": 1, "PROG": "sb"}

        def launcher(*a, **k):
            code, out, err = self.run_cli("stop", "coder7", "--reason", "retire")
            return code, json.loads(out), err
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(sbcw.H, over))
            for target, value in (("_may_retire", lambda k, r: True), ("_registry_note", lambda *a: None), ("Cluster", mock.MagicMock), ("run_launcher", launcher),
                                  ("remove_record", lambda st, n: os.unlink(record))):
                stack.enter_context(mock.patch.object(sbcw, target, value))
            stack.enter_context(contextlib.redirect_stdout(out))
            code = sbcw.cmd_retire(types.SimpleNamespace(name="coder7", reason="done", undo=False), None, None, {"runtime_id": "pod/coder7"})
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(record))
        self.assertEqual(self.st()["secrets"], {"other-worker-pw": 1})
        freed = [a for a in audit if a[0] == "cloud.name_freed"]
        self.assertEqual(len(freed), 1)
        self.assertEqual(len(freed[0][2]["secrets_removed"]), 2)
        self.assertIn("Secrets removed", out.getvalue())
        self.create("coder7")   # the same name launches again
        self.assertEqual(list(self.st()["pods"]), [tcw.cw.pod_name("coder7")])

    def test_a_pod_not_found_message_must_name_this_pod(self):
        self.create("coder7")
        plant_run_secret(self)
        st = self.st()
        st["pods"].pop(tcw.cw.pod_name("coder7"))
        self.write_state(st)
        real = tcw.cw.Kube.run

        def other(kube, args, *a, **k):   # NotFound for another object: not this pod's absence
            if args[:2] == ["get", "pod"]:
                raise tcw.cw.CloudError("kubectl get: Error from server (NotFound): pods \"someone-else\" not found")
            return real(kube, args, *a, **k)
        with mock.patch.object(tcw.cw.Kube, "run", other):
            code, out, err = self.run_cli("stop", "coder7", "--reason", "retire")
        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.st()["secrets"]), 2)


class Retire(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(self.state, "cloud"))
        self.audit = []
        engine = types.SimpleNamespace(actor="op", audit=types.SimpleNamespace(append=lambda actor, ev, name, d: self.audit.append((ev, name, d))))
        H = {"LaunchError": RuntimeError, "build_engine": lambda r, n, c: (engine, self.state, None),
             "gates_caller_kind": lambda r, n: ("person", "x"), "EXIT_OK": 0, "EXIT_ERROR": 1, "PROG": "sb"}
        for key, value in H.items():
            patcher = mock.patch.dict(sbcw.H, {key: value})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.record = os.path.join(self.state, "cloud", "coder7.json")
        self.touch()
        for target, value in (("_may_retire", lambda k, r: True), ("_registry_note", lambda *a: None), ("Cluster", mock.MagicMock)):
            patcher = mock.patch.object(sbcw, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        remover = mock.patch.object(sbcw, "remove_record", lambda state, name: os.unlink(self.record) if os.path.exists(self.record) else None)
        remover.start()
        self.addCleanup(remover.stop)

    def touch(self):
        with open(self.record, "w") as handle:
            handle.write("{}")

    def retire(self, reply=None, raises=None):
        args = types.SimpleNamespace(name="coder7", reason="done", undo=False)
        out = io.StringIO()
        err = io.StringIO()
        def launcher(*a, **k):
            if raises:
                raise raises
            return reply
        with mock.patch.object(sbcw, "run_launcher", launcher), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = sbcw.cmd_retire(args, None, None, {"runtime_id": "pod/coder7"})
            except RuntimeError:
                code = 1
        return code, out.getvalue()

    def freed(self):
        return [a for a in self.audit if a[0] == "cloud.name_freed"]

    def test_s7_gone_pod_frees_the_name_with_one_audit_line(self):
        code, out = self.retire((0, {"runtime_id": "", "pushed": False, "pod_gone": True}, ""))
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.record))
        self.assertEqual(len(self.freed()), 1)
        self.assertEqual(self.freed()[0][2]["why"], "pod gone")
        self.assertIn("already gone", out)

    def test_s8_hook_cannot_push_frees_nothing(self):
        code, out = self.retire((1, {"pushed": False}, "no remote"))
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(self.record))
        self.assertEqual(self.freed(), [])

    def test_s9_unreadable_pod_keeps_the_record(self):
        for reply, raises in (((3, None, "kubectl error"), None), ((0, None, ""), None), (None, sbcw.CloudError("timeout"))):
            self.touch()
            code, _ = self.retire(reply, raises)
            if reply and reply[0] == 0:   # a malformed reply (no body) is a normal stop, never a free
                self.assertEqual(self.freed(), [])
                continue
            self.assertNotEqual(code, 0)
            self.assertTrue(os.path.exists(self.record))
            self.assertEqual(self.freed(), [])

    def test_u3_live_pod_normal_retire_unchanged_no_name_freed_line(self):
        code, out = self.retire((0, {"runtime_id": "pod/coder7", "pushed": False}, ""))
        self.assertEqual(code, 0)
        self.assertIn("its pod and scratch volume are deleted", out)
        self.assertFalse(os.path.exists(self.record))
        self.assertEqual(self.freed(), [])

    def test_a_truthy_non_true_field_is_not_gone(self):
        self.retire((0, {"runtime_id": "pod/coder7", "pod_gone": "yes"}, ""))
        self.assertEqual(self.freed(), [])


if __name__ == "__main__":
    unittest.main()
