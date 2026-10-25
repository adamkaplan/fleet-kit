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


class Launcher(tcw.Base):
    def test_gone_pod_stop_says_so_and_touches_nothing_else(self):
        self.create("coder7")
        st = self.st()
        st["pods"].pop(tcw.cw.pod_name("coder7"))   # an operator deleted the pod; both Secrets stay
        self.write_state(st)
        self.assertTrue(self.st()["secrets"])
        code, out, err = self.run_cli("stop", "coder7", "--reason", "retire")
        self.assertEqual(code, 0, err)
        self.assertIs(json.loads(out).get("pod_gone"), True)
        self.assertTrue(self.st()["secrets"])   # by name nothing is deleted (pinned by test_cloud_stop_nonce)

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

    def test_u3_live_pod_normal_retire_prints_deleted_and_frees_nothing(self):
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
