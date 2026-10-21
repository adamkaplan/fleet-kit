#!/usr/bin/env python3
"""The cloud status summary and the clearing command (ask #1056): ONE cluster read per status run, a record whose pod is gone shown as ENDED,
and ONE command that clears every such record.

Offline: the fake kubectl beside this file IS the cluster and the real launcher reads it. No cloud, no network, no real credential.
"""
import argparse
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

os.environ.setdefault("FLEET_SWITCHBOARD_UNDER_TEST", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
KIT = os.path.dirname(os.path.dirname(LAUNCHER))
BIN = os.path.join(KIT, "bin")

loader = importlib.machinery.SourceFileLoader("cw_fleet_switchboard", os.path.join(BIN, "fleet-switchboard"))
spec = importlib.util.spec_from_loader("cw_fleet_switchboard", loader)
sb = importlib.util.module_from_spec(spec)
sys.modules["cw_fleet_switchboard"] = sb
loader.exec_module(sb)

DEAD = "2020-01-01T00:00:00Z"      # a hard deadline that has passed
ALIVE = "2099-01-01T00:00:00Z"     # a hard deadline that has not


class Base(unittest.TestCase):
    """The fake cluster (the fake kubectl's state file), the switchboard's state directory and the records in it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.state_dir = self.home / "state" / "fleet-switchboard"
        (self.state_dir / "cloud").mkdir(parents=True)
        os.chmod(str(self.state_dir), 0o700)
        os.chmod(str(self.state_dir / "cloud"), 0o700)
        self.kube = self.home / "kube.json"
        self.calls = self.home / "calls.jsonl"
        self.write_cluster({"pods": {}, "nodes": []})
        token = self.home / "token.sh"
        token.write_text("#!/bin/sh\necho synthetic-cluster-token\n")
        token.chmod(0o700)
        config = self.home / "cfg" / "fleet-switchboard" / "config.json"
        config.parent.mkdir(parents=True)
        fake = self.home / "opencode"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)
        config.write_text(json.dumps({"opencode": str(fake), "herdr": str(self.home / "no-herdr")}))
        (self.home / "tmp").mkdir()
        patcher = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "cfg"), "XDG_STATE_HOME": str(self.home / "state"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": str(self.home / "tmp"),
            "FLEET_CLOUD_WORKER_BIN": os.path.join(LAUNCHER, "cloud-worker"),
            "CW_ALLOWED_REPOS": "acme-org/widget-service", "CW_CLOUD_IMAGE": "registry.example.internal/worker@sha256:" + "0" * 64,
            "CW_CLOUD_SERVER": "https://cluster.example.internal:6443", "CW_CLOUD_NAMESPACE": "cw",
            "CW_VAULT_SUBSCRIPTION": "00000000-0000-0000-0000-000000000000", "CW_VAULT_NAME": "vault-example",
            "CW_KUBECTL": os.path.join(HERE, "fake_kubectl"), "FAKE_KUBE_STATE": str(self.kube), "FAKE_KUBE_CALLS": str(self.calls),
            "CW_CLUSTER_TOKEN_CMD": str(token),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("HERDR_PANE_ID", "OPENCODE_SESSION_ID", "CW_KUBE_CONTEXT", "CW_KUBECONFIG", "CW_CLUSTER_ADMIN_CMD"):
            os.environ.pop(name, None)
        for name, value in (("gates_caller_kind", lambda runner, now: ("person", "a person at a shell")), ("ENSURE_ON_USE", False)):
            patch = mock.patch.object(sb, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    # ---- the fake cluster

    def write_cluster(self, state):
        with open(self.kube, "w") as f:
            json.dump(state, f)

    def cluster(self):
        with open(self.kube) as f:
            return json.load(f)

    def node(self, name, pool, size, created):
        return {"metadata": {"name": name, "labels": {"agentpool": pool, "node.kubernetes.io/instance-type": size},
                              "creationTimestamp": created}}

    def pod(self, name, worker, phase="Running", node=None, hard=DEAD):
        return {"metadata": {"name": name, "labels": {"app": "cw-worker"},
                             "annotations": {"cw/worker": worker, "cw/hard-deadline": hard}},
                "spec": {"nodeName": node}, "status": {"phase": phase}}

    def put_pods(self, *pods):
        state = self.cluster()
        state.setdefault("pods", {})
        for pod in pods:
            state["pods"][pod["metadata"]["name"]] = pod
        self.write_cluster(state)

    def put_nodes(self, *nodes):
        state = self.cluster()
        state["nodes"] = list(nodes)
        self.write_cluster(state)

    def calls_to(self, resource):
        """The fake kubectl calls that read `resource` (the login flags come first)."""
        found = []
        for call in self.kube_calls():
            if "get" in call and call[call.index("get") + 1:][:1] == [resource]:
                found.append(call)
        return found

    def kube_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines() if line.strip()]

    # ---- the records

    def record(self, name, hard=DEAD, reports_to="orch", pod=None, password=True):
        body = {"worker": name, "url": "https://worker-%s.example.internal:4096" % name, "runtime_id": "pod/" + (pod or ("cw-" + name)),
                "idle_deadline": DEAD, "hard_deadline": hard, "session": "ses_" + name, "reports_to": reports_to, "agent": "coder"}
        path = self.state_dir / "cloud" / (name + ".json")
        path.write_text(json.dumps(body))
        os.chmod(str(path), 0o600)
        if password:
            secret = self.state_dir / "cloud" / (name + ".password")
            secret.write_text("synthetic-password\n")
            os.chmod(str(secret), 0o600)
        return path

    def records_left(self):
        return sorted(p.name for p in (self.state_dir / "cloud").glob("*.json"))

    # ---- the commands

    def cloud(self):
        return sb.load_module("cloud_worker").bind(sb.__dict__)

    def status(self):
        """The cloud lines and rows of `status` (what `cmd_status` prints)."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            lines, rows = self.cloud().status_extra(subprocess.run)
        return lines, rows

    def run_command(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = sb.main(list(argv), runner=subprocess.run, now=time.time)
        return code, out.getvalue(), err.getvalue()

    def clear(self, *extra):
        return self.run_command("cloud-clear", *extra)


class TestSummary(Base):
    """S2, S3, S5: one cloud summary line from one cluster read, and the ended wording."""

    def the_cluster(self):
        """3 nodes in 2 pools (2 and 1) of two sizes, and 7 pods (5 Running, 1 Pending, 1 Failed)."""
        self.put_nodes(self.node("node-a", "pool-a", "size-1", "2026-10-14T07:00:00Z"),
                       self.node("node-b", "pool-a", "size-1", "2026-10-18T07:00:00Z"),
                       self.node("node-c", "pool-b", "size-2", "2026-10-20T11:20:00Z"))
        pods = [self.pod("cw-w%d" % i, "w%d" % i, phase=phase, node=node)
                for i, (phase, node) in enumerate([("Running", "node-a"), ("Running", "node-a"), ("Running", "node-b"),
                                                   ("Running", "node-a"), ("Running", "node-c"), ("Pending", "node-c"),
                                                   ("Failed", "node-a")])]
        self.put_pods(*pods)

    def test_s2a_the_summary_line_names_the_pools_the_sizes_the_ages_and_the_phases(self):
        self.the_cluster()
        self.record("coder7")
        lines, _ = self.status()
        summary = [line.strip() for line in lines if line.strip().startswith("cloud: nodes")]
        self.assertEqual(len(summary), 1, lines)
        self.assertIn("2 x", summary[0])
        self.assertIn("Running 5", summary[0])
        self.assertIn("Pending 1", summary[0])
        self.assertIn("Failed 1", summary[0])
        self.assertIn("pool-a", summary[0])
        self.assertIn("pool-b", summary[0])
        self.assertIn("size-1", summary[0])
        self.assertIn("size-2", summary[0])
        self.assertRegex(summary[0], r"\d+d\d+h|\d+h\d+m|\d+m")   # an age
        self.assertIn("pods 7", summary[0])

    def test_s2b_thirty_five_records_cost_exactly_one_snapshot_read(self):
        self.the_cluster()
        for i in range(35):
            self.record("w%d" % i)
        lines, _ = self.status()
        self.assertEqual(len(self.calls_to("nodes")), 1, self.kube_calls())
        self.assertEqual(len(self.calls_to("pods")), 1, self.kube_calls())

    def test_s2c_a_cluster_that_cannot_be_read_says_so_instead_of_hanging(self):
        self.record("coder7")
        state = self.cluster()
        state["fail_cmd"] = "get"
        self.write_cluster(state)
        os.environ["FAKE_KUBE_ECHO"] = "the cluster is down"
        self.addCleanup(os.environ.pop, "FAKE_KUBE_ECHO", None)
        lines, _ = self.status()
        failed = [line.strip() for line in lines if line.strip().startswith("cloud: cluster not read (")]
        self.assertEqual(len(failed), 1, lines)
        self.assertIn("the cluster is down", failed[0])
        self.assertIn("nodes and pods unknown", failed[0])

    def test_s2c_a_read_that_hangs_returns_within_the_bound(self):
        self.record("coder7")
        os.environ["FAKE_KUBE_HANG"] = "get"
        os.environ["FAKE_KUBE_HANG_MARK"] = str(self.home / "hang")
        self.addCleanup(os.environ.pop, "FAKE_KUBE_HANG", None)
        self.addCleanup(os.environ.pop, "FAKE_KUBE_HANG_MARK", None)
        self.cloud().SNAPSHOT_SECONDS = 2   # the bound of the whole read, shortened for the test
        started = time.monotonic()
        lines, _ = self.status()
        self.assertLess(time.monotonic() - started, 30)
        failed = [line.strip() for line in lines if line.strip().startswith("cloud: cluster not read (")]
        self.assertEqual(len(failed), 1, lines)

    def test_s3a_a_record_whose_pod_is_gone_is_ended_with_its_deadline(self):
        self.record("coder7", hard=DEAD)
        lines, rows = self.status()
        line = [l for l in lines if "coder7" in l][0]
        self.assertIn("ended at %s" % DEAD, line)
        self.assertNotIn("not queried", line)
        self.assertEqual(rows[0]["launcher_status"], "ended")
        self.assertEqual(rows[0]["ended_at"], DEAD)

    def test_s3b_a_record_whose_pod_still_stands_says_so(self):
        self.put_pods(self.pod("cw-coder7", "coder7", phase="Running"))
        self.record("coder7", hard=DEAD)
        lines, rows = self.status()
        line = [l for l in lines if "coder7" in l][0]
        self.assertIn("pod still present past its hard deadline", line)
        self.assertNotIn("ended", line)
        self.assertNotEqual(rows[0].get("launcher_status"), "ended")

    def test_s3b_a_record_of_an_unreadable_cluster_is_never_called_ended(self):
        self.record("coder7", hard=DEAD)
        state = self.cluster()
        state["fail_cmd"] = "get"
        self.write_cluster(state)
        lines, rows = self.status()
        line = [l for l in lines if "coder7" in l][0]
        self.assertIn("not queried", line)
        self.assertNotIn("ended", line)
        self.assertNotEqual(rows[0].get("launcher_status"), "ended")

    def test_s5a_a_login_that_may_not_list_the_nodes_counts_them_by_pod_placement(self):
        self.the_cluster()
        state = self.cluster()
        state["fail_nodes"] = True
        self.write_cluster(state)
        self.record("coder7")
        lines, _ = self.status()
        summary = [line.strip() for line in lines if line.strip().startswith("cloud: nodes")][0]
        self.assertIn("from pod placement", summary)
        self.assertIn("nodes at least 3", summary)   # the distinct node names of the pods (node-a, node-b, node-c)
        self.assertNotIn("cluster not read", summary)
        self.assertIn("pods 7", summary)

    def test_s5b_the_summary_read_makes_no_write_call(self):
        self.the_cluster()
        self.record("coder7")
        self.status()
        writes = [c for c in self.kube_calls() if c[0] in ("create", "patch", "delete", "label", "exec", "apply", "port-forward")]
        self.assertEqual(writes, [], writes)

    def test_u3_no_credential_reaches_the_output(self):
        self.the_cluster()
        self.record("coder7")
        lines, rows = self.status()
        for text in lines + [json.dumps(rows)]:
            self.assertNotIn("synthetic-cluster-token", text)
            self.assertNotIn("synthetic-password", text)

    def test_the_status_command_prints_the_summary_line(self):
        self.the_cluster()
        self.record("coder7")
        code, out, err = self.run_command("status")
        self.assertEqual(len([l for l in out.splitlines() if l.strip().startswith("cloud: nodes")]), 1, out)


class TestClear(Base):
    """S4: one command clears the records whose pod is gone and whose deadline has passed."""

    def the_fleet(self):
        """3 records whose pod is gone and whose deadline has passed, 1 with a pod, 1 not past its deadline."""
        for name in ("gone1", "gone2", "gone3"):
            self.record(name, hard=DEAD)
        self.record("alive", hard=DEAD, pod="cw-alive")
        self.put_pods(self.pod("cw-alive", "alive", phase="Running"))
        self.record("fresh", hard=ALIVE)

    def test_s4a_it_clears_exactly_the_ended_records_and_names_them(self):
        self.the_fleet()
        code, out, err = self.clear()
        self.assertEqual(code, 0, err)
        cleared = [l for l in out.splitlines() if l.startswith("cleared ")]
        self.assertEqual(len(cleared), 3, out)
        for name in ("gone1", "gone2", "gone3"):
            self.assertTrue(any(l.startswith("cleared %s " % name) for l in cleared), out)
        self.assertEqual(self.records_left(), ["alive.json", "fresh.json"])
        self.assertFalse((self.state_dir / "cloud" / "gone1.password").exists())

    def test_s4a_it_never_touches_a_record_with_a_pod(self):
        self.the_fleet()
        self.clear()
        self.assertTrue((self.state_dir / "cloud" / "alive.json").exists())
        self.assertTrue((self.state_dir / "cloud" / "alive.password").exists())
        self.assertTrue((self.state_dir / "cloud" / "fresh.json").exists())

    def test_s4b_the_retire_of_a_record_whose_pod_is_gone_ends_cleanly(self):
        self.record("gone1", hard=DEAD)
        code, out, err = self.run_command("retire", "gone1", "--reason", "done")
        self.assertEqual(code, 0, err)
        self.assertFalse((self.state_dir / "cloud" / "gone1.json").exists())

    def test_s4c_it_refuses_when_the_cluster_could_not_be_read(self):
        self.the_fleet()
        state = self.cluster()
        state["fail_cmd"] = "get"
        self.write_cluster(state)
        code, out, err = self.clear()
        self.assertEqual(code, 1)
        self.assertIn("could not be read", out + err)
        self.assertEqual(len(self.records_left()), 5)

    def test_s4c_a_caller_who_is_not_the_boss_gets_nothing(self):
        self.the_fleet()
        with mock.patch.object(sb, "gates_caller_kind", lambda runner, now: ("agent", "coder9", "coder")):
            code, out, err = self.clear()
        self.assertEqual(code, 0, err)
        self.assertIn("skipped gone1 (not your worker)", out)
        self.assertEqual(len(self.records_left()), 5)

    def test_s4c_the_boss_of_a_worker_may_clear_it(self):
        self.record("mine", hard=DEAD, reports_to="orch")
        with mock.patch.object(sb, "gates_caller_kind", lambda runner, now: ("agent", "orch", "orchestrator")):
            code, out, err = self.clear()
        self.assertEqual(code, 0, err)
        self.assertIn("cleared mine", out)
        self.assertEqual(self.records_left(), [])

    def test_s4d_the_dry_run_removes_nothing(self):
        self.the_fleet()
        code, out, err = self.clear("--dry-run")
        self.assertEqual(code, 0, err)
        for name in ("gone1", "gone2", "gone3"):
            self.assertIn("would clear %s" % name, out)
        self.assertEqual(len(self.records_left()), 5)

    def test_u2_no_command_path_removes_a_record_that_has_a_pod(self):
        self.the_fleet()
        self.clear()
        self.assertTrue((self.state_dir / "cloud" / "alive.json").exists())


class TestDocs(Base):
    """S7: the documentation describes the summary line, the failure line, the ended wording, the clearing command and the one-verb node rule."""

    FILES = (os.path.join(KIT, "docs", "switchboard.md"), os.path.join(KIT, "docs", "cloud-worker-interface.md"),
             os.path.join(KIT, "cloud", "launcher", "README.md"))

    def test_the_docs_describe_the_summary_the_failure_the_ended_wording_and_the_clearing_command(self):
        text = "\n".join(Path(path).read_text() for path in self.FILES)
        for wanted in ("cloud-clear", "--dry-run", "cluster not read", "ended at", "from pod placement"):
            self.assertTrue(wanted in text, wanted)

    def test_the_docs_state_the_one_verb_node_rule(self):
        text = "\n".join(Path(path).read_text() for path in self.FILES)
        flat = " ".join(text.split())
        self.assertTrue("list on nodes" in flat, "the one-verb node rule is not described")


if __name__ == "__main__":
    unittest.main()
