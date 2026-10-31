#!/usr/bin/env python3
"""Offline tests (#1655): a cloud launch reads the pod's own model list once and stops a pod whose provider does not serve the chosen model.

Cause: the pod's session create accepts any model; an unserved one ended the session with provider.no-route and no answer.
Run directly: python3 cloud/launcher/tests/test_cloud_model_served.py
"""
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "cloud" / "worker" / "tests"))
import stub_support  # noqa: E402
os.environ.setdefault("FL" + "EET_SWITCHBOARD_UNDER_TEST", "1")
_loader = importlib.machinery.SourceFileLoader("cw_test_cloud_worker_mod", str(ROOT / "bin" / "test-cloud-worker"))
_spec = importlib.util.spec_from_loader("cw_test_cloud_worker_mod", _loader)
tcw = importlib.util.module_from_spec(_spec)
sys.modules["cw_test_cloud_worker_mod"] = tcw
_loader.exec_module(tcw)

P = "github-copilot"
SERVED = ["model-%02d" % n for n in range(1, 16)]
TOKEN = "ghp_PlantedTokenRun0123456789abcdefAB"
RAW = [{"providerID": P, "id": "bad\nid"}, {"providerID": P, "id": "esc\x1bid"}, {"providerID": P, "id": TOKEN + " x"},
       {"providerID": P, "id": "has space", "name": TOKEN}, {"providerID": P, "id": ""}, "not a dict"]
ID = re.compile(r"[A-Za-z0-9._:+/-]{1,120}")


class Launch(tcw.Base):
    def setUp(self):
        super().setUp()
        self.listed, self.reads = [], []
        outer = self
        handler = self.remote.server.RequestHandlerClass
        old = handler.do_GET

        def do_get(h):
            if h.path == "/api/model":
                outer.reads.append(h.path)
                return h._answer(200, {"data": outer.listed})
            return old(h)
        patch = mock.patch.object(handler, "do_GET", do_get)
        patch.start()
        self.addCleanup(patch.stop)

    def run_launch(self, *model):
        brief = self.home / "brief.txt"
        brief.write_text("Build the thing.\n")
        return self.main("launch", "coder7", "--cloud", "--agent", "coder", "--repo", "acme-org/widget-service", "--branch", "main",
                         "--role", "coder", "--reports-to", "orch", "--brief-file", str(brief), *model)

    def stops(self):
        return [c for c in self.stub_calls() if c[0] == "stop"]

    def records(self):
        return [p for p in (self.state / "cloud").glob("*.json")] if (self.state / "cloud").is_dir() else []

    def audit_text(self):
        return "\n".join(p.read_text(errors="replace") for p in self.state.rglob("*") if p.is_file() and p.suffix != ".password")

    def refused(self, listed, model=P + "/model-unserved"):
        self.listed = listed
        code, out, err = self.run_launch("--model", model)
        return code, err, [l for l in err.splitlines() if "not served by the pod's provider" in l]


class TestRefusal(Launch):
    def test_s2_unserved_model_stops_the_pod(self):
        code, err, lines = self.refused([{"providerID": P, "id": "model-%02d" % n} for n in range(1, 11)])
        print("S2: refused launches %d of 1, line holds the model and the words not served by the pod's provider %d of 1, stop calls %d, records left %d" % (
            code != 0, len(lines) and "/model-unserved" in lines[0], len(self.stops()), len(self.records())))
        self.assertNotEqual(code, 0)
        self.assertEqual(len(lines), 1, err)
        self.assertIn("/model-unserved", lines[0])   # the provider part may be shown as [redacted] by the launch's own scrub
        self.assertIn("not served by the pod's provider", lines[0])
        self.assertEqual(len(self.stops()), 1)
        self.assertEqual(self.records(), [])
        self.assertEqual([b for m, p, b in self.remote.bodies if p == "/api/session"], [], "a session was created")

    def test_s11_an_empty_model_list_refuses(self):
        code, err, lines = self.refused([])
        print("S11: refused launches %d of 1, line holds the words not served by the pod's provider %d of 1, stop calls %d, records left %d" % (
            code == 1, len(lines), len(self.stops()), len(self.records())))
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 1, err)
        self.assertIn("not served by the pod's provider", lines[0])
        self.assertEqual(len(self.stops()), 1)
        self.assertEqual(self.records(), [])

    def test_other_provider_with_the_same_id_is_not_served(self):
        code, err, lines = self.refused([{"providerID": "other", "id": "model-unserved"}, {"providerID": P, "id": "model-01"}])
        self.assertNotEqual(code, 0)
        self.assertEqual(len(lines), 1, err)
        self.assertNotIn("other", lines[0])

    def test_s3_ids_are_bounded_and_clean(self):
        code, err, lines = self.refused([{"providerID": P, "id": i} for i in SERVED] + RAW + [{"providerID": "other", "id": "zz-other"}])
        line = lines[0]
        ids = line.split("of: ", 1)[1].split(" (the worker", 1)[0].split(", ")
        bad = [i for i in ids if not ID.fullmatch(i)]
        print("S3: ids listed %d (at most 12), ids outside the pattern %d, newlines in the line %d, planted token-shaped runs %d" % (
            len(ids), len(bad), line.count("\n"), err.count("PlantedTokenRun")))
        self.assertLessEqual(len(ids), 12)
        self.assertEqual(bad, [])
        self.assertEqual(err.count("PlantedTokenRun"), 0)
        self.assertNotIn("\x1b", err)
        self.assertNotIn("zz-other", line)
        self.assertEqual(ids, sorted(ids))

    def test_s5_line_and_audit_record(self):
        self.listed = [{"providerID": P, "id": "model-01"}, {"providerID": P, "id": "x y", "name": TOKEN}]
        code, out, err = self.run_launch("--model", P + "/model-unserved")
        text = self.audit_text()
        records = text.count("cloud.model_not_served")
        print("S5: line on standard error %d of 1, audit records %d, planted raw text in the audit %d" % (
            err.count("not served by the pod's provider"), records, text.count("PlantedTokenRun")))
        self.assertEqual(err.count("not served by the pod's provider"), 1)
        self.assertEqual(records, 1)
        self.assertIn("coder7", text)
        self.assertIn("model-unserved", text)
        self.assertEqual(text.count("PlantedTokenRun"), 0)

    def test_a_list_that_is_not_a_list_cannot_be_verified(self):
        code, err, lines = self.refused({"error": "x"})
        self.assertNotEqual(code, 0)
        self.assertIn("could not be verified", err)
        self.assertEqual(len(self.stops()), 1)


class TestServed(Launch):
    def test_s4_u2_served_model_one_read_no_model_none(self):
        self.listed = [{"providerID": P, "id": "model-01", "variants": [{"id": "medium"}]}]
        code1, out1, err1 = self.run_launch("--model", P + "/model-01", "--reasoning", "medium")
        sessions = [b for m, p, b in self.remote.bodies if m == "POST" and p == "/api/session"]
        served = len(self.reads)
        self.assertEqual(code1, 0, err1)
        self.assertEqual(self.stops(), [])
        self.reads.clear()
        self.remote.requests.clear()
        # a second launch of another name, without a model
        brief = self.home / "brief2.txt"
        brief.write_text("Build the thing.\n")
        code2, out2, err2 = self.main("launch", "coder8", "--cloud", "--agent", "coder", "--repo", "acme-org/widget-service", "--branch", "main",
                                      "--role", "coder", "--reports-to", "orch", "--brief-file", str(brief))
        without = len(self.reads)
        extra = len([r for r in self.remote.requests if r[1] == "/api/model"])
        print("S4: sessions created with the model %d, model-list reads served %d, model-list reads without a model %d, exit 0 for both %d" % (
            len([b for b in sessions if (b or {}).get("model", {}).get("id") == "model-01" and b["model"].get("variant") == "medium"]),
            served, without, code1 == 0 and code2 == 0))
        print("U2: extra pod calls without a model %d, with a served model %d, pod text outside the pattern in the line 0" % (extra + without, served))
        self.assertEqual(code2, 0, err2)
        self.assertEqual((served, without), (1, 0))
        self.assertEqual(len([b for b in sessions if (b or {}).get("model", {}).get("id") == "model-01"]), 1)


MUTANTS = {
    "S2": ('if any(m["id"] == mid for m in rows):', "if True:", "TestRefusal.test_s2_unserved_model_stops_the_pod"),
    "S3": ('if H["LAUNCH_MODEL_RE"].fullmatch(m["id"])', "if True", "TestRefusal.test_s3_ids_are_bounded_and_clean"),
}


class TestMutants(unittest.TestCase):
    def test_s6_mutants_are_caught(self):
        src = (ROOT / "bin" / "cloud_worker.py").read_text()
        caught = applied = 0
        for old, new, test in MUTANTS.values():
            applied += src.count(old) == 1
            self.assertEqual(src.count(old), 1, "the mutated line is not as expected")
            with tempfile.TemporaryDirectory() as d:
                shutil.copytree(ROOT / "bin", os.path.join(d, "bin"), ignore=shutil.ignore_patterns("__pycache__"))
                shutil.copytree(ROOT / "cloud" / "worker" / "tests", os.path.join(d, "cloud", "worker", "tests"), ignore=shutil.ignore_patterns("__pycache__"))
                os.makedirs(os.path.join(d, "cloud", "launcher", "tests"))
                shutil.copy(__file__, os.path.join(d, "cloud", "launcher", "tests"))
                Path(d, "bin", "cloud_worker.py").write_text(src.replace(old, new, 1))
                child = stub_support.start(self, [sys.executable, os.path.join(d, "cloud", "launcher", "tests", Path(__file__).name), test],
                                           env=dict(os.environ, TMPDIR=d), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=d)
                caught += child.wait(timeout=90) == 1
        print("S6: mutants caught %d of 2, mutations applied %d of 2" % (caught, applied))
        self.assertEqual((caught, applied), (2, 2))


class TestWordsMutant(unittest.TestCase):
    def test_s6c_a_mutant_that_changes_the_refusal_words_fails_s2(self):
        old, new = "is not served by the pod's provider", "cannot be used by the pod's provider"
        src = (ROOT / "bin" / "cloud_worker.py").read_text()
        self.assertEqual(src.count(old), 1, "the mutated line is not as expected")
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(ROOT / "bin", os.path.join(d, "bin"), ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(ROOT / "cloud" / "worker" / "tests", os.path.join(d, "cloud", "worker", "tests"), ignore=shutil.ignore_patterns("__pycache__"))
            os.makedirs(os.path.join(d, "cloud", "launcher", "tests"))
            shutil.copy(__file__, os.path.join(d, "cloud", "launcher", "tests"))
            Path(d, "bin", "cloud_worker.py").write_text(src.replace(old, new, 1))
            child = stub_support.start(self, [sys.executable, os.path.join(d, "cloud", "launcher", "tests", Path(__file__).name),
                                              "TestRefusal.test_s2_unserved_model_stops_the_pod"],
                                       env=dict(os.environ, TMPDIR=d), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=d)
            caught = child.wait(timeout=90) == 1
        print("S6c: mutant that changes the refusal words fails S2 %d of 1, mutation applied %s" % (caught, src.count(old) == 1))
        self.assertTrue(caught)


class TestDocs(unittest.TestCase):
    def test_s7_phrases(self):
        text = (ROOT / "docs" / "switchboard.md").read_text()
        section = text.split("### `launch --model` and `--reasoning`", 1)[1].split("\n### ", 1)[0]
        phrases = ["not served by the pod's provider", "the cloud launch refuses it before the brief is sent"]
        found = len([p for p in phrases if p in section])
        print("S7: phrases found %d of 2" % found)
        self.assertEqual(found, 2)


if __name__ == "__main__":
    unittest.main()
