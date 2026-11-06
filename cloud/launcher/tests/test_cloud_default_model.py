#!/usr/bin/env python3
"""Offline tests: a non-orchestrator cloud launch with no --model gets github-copilot/claude-sonnet-5.5 at level medium; any other choice is audited.

Run directly: python3 cloud/launcher/tests/test_cloud_default_model.py
"""
import importlib.machinery
import importlib.util
import json
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "cloud" / "worker" / "tests"))
import stub_support  # noqa: E402
os.environ.setdefault("FL" + "EET_SWITCHBOARD_UNDER_TEST", "1")
_loader = importlib.machinery.SourceFileLoader("cw_test_default_model_mod", str(ROOT / "bin" / "test-cloud-worker"))
_spec = importlib.util.spec_from_loader("cw_test_default_model_mod", _loader)
tcw = importlib.util.module_from_spec(_spec)
sys.modules["cw_test_default_model_mod"] = tcw
_loader.exec_module(tcw)

PART = ROOT / "bin" / "switchboard" / "launch_model.py"


def load_part(text):
    ns = {"re": re, "LaunchError": Exception, "_one_line": lambda t, n: t[:n]}
    exec(text, ns)
    return ns


DEFAULT = {"providerID": "github-copilot", "id": "claude-sonnet-5.5", "variant": "medium"}


class DefaultModel(tcw.Base):
    def setUp(self):
        super().setUp()
        self.audit = self.home / "launcher-audit.jsonl"
        os.environ["CW_CLOUD_AUDIT"] = str(self.audit)
        self.addCleanup(os.environ.pop, "CW_CLOUD_AUDIT", None)
        handler = self.remote.server.RequestHandlerClass
        old = handler.do_GET
        from unittest import mock

        def do_get(h):
            if h.path == "/api/model":
                return h._answer(200, {"data": [{"providerID": "github-copilot", "id": i} for i in ("claude-sonnet-5.5", "claude-opus-5.5")]})
            return old(h)
        patch = mock.patch.object(handler, "do_GET", do_get)
        patch.start()
        self.addCleanup(patch.stop)

    def bodies(self):
        return [b for m, p, b in self.remote.bodies if m == "POST" and p == "/api/session"]

    def lines(self):
        return [json.loads(l) for l in self.audit.read_text().splitlines()] if self.audit.exists() else []

    def nondefault(self):
        return [l for l in self.lines() if l.get("action") == "model-nondefault"]

    def test_no_model_gets_the_default_at_medium(self):
        code, out, err = self.launch_with("--cloud")
        self.assertEqual(code, 0, err)
        (body,) = self.bodies()
        self.assertEqual(body["model"], DEFAULT)
        self.assertEqual(self.nondefault(), [])

    def test_an_explicit_model_is_unchanged_and_audited_once(self):
        code, out, err = self.launch_with("--cloud", "--model", "github-copilot/claude-opus-5.5", "--reasoning", "high")
        self.assertEqual(code, 0, err)
        (body,) = self.bodies()
        self.assertEqual(body["model"], {"providerID": "github-copilot", "id": "claude-opus-5.5", "variant": "high"})
        (line,) = self.nondefault()
        self.assertEqual((line["worker"], line["model"], line["level"]), ("coder7", "github-copilot/claude-opus-5.5", "high"))

    def test_an_orchestrator_launch_keeps_todays_behaviour(self):
        """The stub launcher takes no --agent, so this is the function the launch calls (loaded like the Mutant test below)."""
        ns = load_part(PART.read_text())
        calls = []
        orch = lambda **kw: type("A", (), dict({"model": None, "reasoning": None, "agent": "orchestrator", "name": "o"}, **kw))()
        self.assertEqual(ns["launch_model_cloud"](orch(), "github-copilot", lambda *a: calls.append(a)), {})
        self.assertEqual(calls, [])
        self.assertEqual(ns["launch_model_cloud"](orch(model="github-copilot/claude-opus-5.5"), "github-copilot", lambda *a: calls.append(a)),
                         {"model": "github-copilot/claude-opus-5.5"})
        self.assertEqual(calls, [("o", "github-copilot/claude-opus-5.5", None)])

    def test_the_default_choice_is_in_launch_model_py(self):
        text = (ROOT / "bin" / "switchboard" / "launch_model.py").read_text()
        self.assertRegex(text, r'CLOUD_DEFAULT_MODEL, CLOUD_DEFAULT_LEVEL = "github-copilot/claude-sonnet-5\.5", "medium"')


class Mutant(unittest.TestCase):
    def test_a_launch_model_without_the_default_fails_with_an_anchored_text(self):
        path = ROOT / "bin" / "switchboard" / "launch_model.py"
        text = path.read_text()
        mutant = text.replace('else {"model": CLOUD_DEFAULT_MODEL, "reasoning": CLOUD_DEFAULT_LEVEL}', "else {}")
        self.assertNotEqual(mutant, text, "the mutation did not apply")
        args = type("A", (), {"model": None, "reasoning": None, "agent": "coder", "name": "w"})()
        control, broken = load_part(text), load_part(mutant)
        self.assertEqual(control["launch_model_cloud"](args, "github-copilot"), {"model": "github-copilot/claude-sonnet-5.5", "reasoning": "medium"})
        self.assertEqual(broken["launch_model_cloud"](args, "github-copilot"), {}, "default missing: no model for a worker launch")


if __name__ == "__main__":
    unittest.main()
