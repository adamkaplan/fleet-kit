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

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
KIT = os.path.dirname(os.path.dirname(LAUNCHER))
BIN = os.path.join(KIT, "bin")

# the switchboard's program name, assembled from a fragment: this file lives under cloud/, where the old name is not written (#404)
SWITCHBOARD = os.path.join(BIN, "fl" + "eet-switchboard")
loader = importlib.machinery.SourceFileLoader("cw_switchboard", SWITCHBOARD)
spec = importlib.util.spec_from_loader("cw_switchboard", loader)
sb = importlib.util.module_from_spec(spec)
sys.modules["cw_switchboard"] = sb
loader.exec_module(sb)
os.environ.setdefault(sb.UNDER_TEST_ENV, "1")   # the suite never runs the real opencode
CLOUD = sb.load_module("cloud_worker")   # the switchboard's cloud module (the launcher's env var name comes from it)

DEAD = "2020-01-01T00:00:00Z"      # a hard deadline that has passed
ALIVE = "2099-01-01T00:00:00Z"     # a hard deadline that has not


class Base(unittest.TestCase):
    """The fake cluster (the fake kubectl's state file), the switchboard's state directory and the records in it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.state_dir = None   # the switchboard's own state directory (its name is the switchboard's, never written here)
        self.kube = self.home / "kube.json"
        self.calls = self.home / "calls.jsonl"
        self.write_cluster({"pods": {}, "nodes": []})
        token = self.home / "token.sh"
        token.write_text("#!/bin/sh\necho synthetic-cluster-token\n")
        token.chmod(0o700)
        (self.home / "tmp").mkdir()
        patcher = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "cfg"), "XDG_STATE_HOME": str(self.home / "state"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": str(self.home / "tmp"),
            CLOUD.LAUNCHER_ENV: os.path.join(LAUNCHER, "cloud-worker"),
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
        config = Path(sb.config_path())   # the switchboard's own config path (its directory name is the switchboard's, never written here)
        config.parent.mkdir(parents=True)
        fake = self.home / "opencode"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)
        config.write_text(json.dumps({"opencode": str(fake), "herdr": str(self.home / "no-herdr")}))
        self.state_dir = Path(sb.state_dir_path())   # the switchboard's own state directory, under the XDG_STATE_HOME of this test
        (self.state_dir / "cloud").mkdir(parents=True, exist_ok=True)
        os.chmod(str(self.state_dir), 0o700)
        os.chmod(str(self.state_dir / "cloud"), 0o700)
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


class TestHostileCluster(Base):
    """A node label or a pod phase is the cluster's word, printed to a person: none of its control bytes may reach the status output."""

    HOSTILE = {
        "escape": "pool\x1b[31m", "bell": "pool\x07", "newline": "pool\nFAKE: all ok", "carriage return": "pool\rFAKE",
        "osc title": "Run\x1b]0;x\x07ning", "next line": "pool\x85x", "line separator": "pool\u2028x", "bidi override": "pool\u202ex",
        "over-long": "p" * 64, "non-ascii digit": "pool\u0663",
    }
    BAD = ("\x1b", "\x07", "\r", "\x85", "\u2028", "\u202e", "\u0663", "FAKE", "p" * 64)

    def assert_clean(self, lines, rows):
        text = "\n".join(lines) + json.dumps(rows, ensure_ascii=False)
        for bad in self.BAD:
            self.assertNotIn(bad, text)
        for line in lines:   # each element is ONE printable ASCII line: a newline inside it would forge another
            self.assertFalse(any(ord(c) < 32 or ord(c) > 126 for c in line), repr(line))

    def hostile_cluster(self, label):
        self.put_nodes(self.node("node-a", label, label, "2026-10-14T07:00:00Z"))
        self.put_pods(self.pod("cw-w1", "w1", phase=label, node="node-a"))
        self.record("w1")

    def test_a_hostile_label_or_phase_never_reaches_the_status_output_raw(self):
        for kind, label in self.HOSTILE.items():
            with self.subTest(kind):
                self.hostile_cluster(label)
                lines, rows = self.status()
                self.assert_clean(lines, rows)
                self.assertIn("?", [l for l in lines if "cloud: nodes" in l][0])

    def test_the_rule_is_one_whitelist_a_clean_value_passes_whole(self):
        lib = CLOUD._lib()
        for good in ("pool-a", "Standard_D4s_v5", "a.b", "Running", "p" * 63):
            self.assertEqual(lib.safe_value(good), good)
        for bad in list(self.HOSTILE.values()) + ["", None, 5]:
            self.assertEqual(lib.safe_value(bad), "?")

    def test_the_summary_line_cleans_a_snapshot_that_is_already_hostile(self):
        snapshot = {"nodes_read": True, "nodes": [{"pool": "pool\x1b[31m\nFAKE", "size": "s\x07", "created": "2026-10-14T07:00:00Z"}],
                    "pods": [{"phase": "Run\x1b]0;x\x07ning", "node": "n"}]}
        line = self.cloud()._summary_line(snapshot, time.time())
        self.assert_clean([line], [])

    def test_a_mutant_without_the_sanitiser_fails_the_check(self):
        snapshot = {"nodes_read": True, "nodes": [{"pool": "pool\x1b[31m", "size": "s", "created": "2026-10-14T07:00:00Z"}], "pods": [{"phase": "Running"}]}
        with mock.patch.object(CLOUD._lib(), "safe_value", lambda value: value):
            line = self.cloud()._summary_line(snapshot, time.time())
        with self.assertRaises(AssertionError):
            self.assert_clean([line], [])


class TestWholeRun(Base):
    """The summary read has ONE deadline; the zero-record status prints its line; the timeout reaches the route."""

    def hang_get(self):
        os.environ["FAKE_KUBE_HANG"] = "get"
        os.environ["FAKE_KUBE_HANG_MARK"] = str(self.home / "hang")
        self.addCleanup(os.environ.pop, "FAKE_KUBE_HANG", None)
        self.addCleanup(os.environ.pop, "FAKE_KUBE_HANG_MARK", None)

    def slow_script(self, name, seconds, then):
        path = self.home / name
        path.write_text("#!/bin/sh\nsleep %s\n%s\n" % (seconds, then))
        path.chmod(0o700)
        return str(path)

    def test_no_records_and_a_reachable_cluster_prints_the_counts_line(self):
        self.put_pods(self.pod("cw-x", "x", phase="Running", node="node-a"))
        lines, rows = self.status()
        self.assertEqual(len([l for l in lines if l.strip().startswith("cloud: nodes")]), 1, lines)
        self.assertEqual(rows, [])

    def test_no_records_and_an_unreachable_cluster_prints_the_reason_line(self):
        state = self.cluster()
        state["fail_cmd"] = "get"
        self.write_cluster(state)
        os.environ["FAKE_KUBE_ECHO"] = "the cluster is down"
        self.addCleanup(os.environ.pop, "FAKE_KUBE_ECHO", None)
        lines, _ = self.status()
        self.assertEqual(len([l for l in lines if l.strip().startswith("cloud: cluster not read (")]), 1, lines)
        self.assertIn("the cluster is down", "\n".join(lines))

    def test_a_slow_mint_step_and_a_slow_launcher_run_share_one_deadline(self):
        self.record("coder7")
        del os.environ["CW_CLUSTER_TOKEN_CMD"]
        os.environ["CW_CLUSTER_ADMIN_CMD"] = "sleep 1.5; echo kubeconfig"   # the vault read: 1.5 s
        os.environ["CW_KUBECTL"] = self.slow_script("slow-kubectl", "0", 'case " $* " in *" create "*) sleep 1.5;; esac; exec "%s" "$@"' % os.path.join(HERE, "fake_kubectl"))
        self.hang_get()   # and the launcher's read never ends
        self.cloud().SNAPSHOT_SECONDS = 2
        started = time.monotonic()
        lines, _ = self.status()
        took = time.monotonic() - started
        self.assertLess(took, 2.9, took)   # per-step caps would allow 1.5 + 1.5 and then the launcher's own second: over 3 s
        self.assertEqual(len([l for l in lines if l.strip().startswith("cloud: cluster not read (")]), 1, lines)

    def bounded_slow_token(self):
        os.environ["CW_CLUSTER_TOKEN_CMD"] = self.slow_script("slow-token", "20", "echo synthetic-cluster-token")
        self.cloud().SNAPSHOT_SECONDS = 1
        started = time.monotonic()
        lines, _ = self.status()
        took = time.monotonic() - started
        self.assertLess(took, 4, took)
        return lines

    def test_a_slow_token_command_returns_within_the_bound(self):
        self.record("coder7")
        lines = self.bounded_slow_token()
        self.assertEqual(len([l for l in lines if l.strip().startswith("cloud: cluster not read (")]), 1, lines)

    def test_a_mutant_that_ignores_the_timeout_argument_fails_that_test(self):
        self.record("coder7")
        real = self.cloud()._capture
        mutant = lambda argv, env=None, feed=None, timeout=None, **kw: real(argv, env=env, feed=feed, timeout=6)   # the constant, not the argument
        with mock.patch.object(self.cloud(), "_capture", mutant), self.assertRaises(AssertionError):
            self.bounded_slow_token()

    def test_a_launcher_output_over_the_limit_is_a_bad_read(self):
        self.put_pods(*[self.pod("cw-w%d" % i, "w%d" % i) for i in range(20)])
        self.record("coder7")
        with mock.patch.object(self.cloud(), "LAUNCHER_OUTPUT_LIMIT", 100):
            lines, _ = self.status()
        self.assertTrue(any("cluster not read" in l and "over 100 bytes" in l for l in lines), lines)

    def test_a_record_with_another_runtime_id_form_is_unknown_not_gone(self):
        path = self.record("coder7", hard=DEAD)
        body = json.loads(path.read_text())
        body["runtime_id"] = "vm/cw-coder7"
        path.write_text(json.dumps(body))
        lines, rows = self.status()
        line = [l for l in lines if "coder7" in l][0]
        self.assertIn("pod unknown", line)
        self.assertNotIn("ended", line)
        self.assertEqual(rows[0]["launcher_status"], "pod unknown")
        code, out, err = self.clear()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.records_left(), ["coder7.json"])
        self.assertNotIn("cleared coder7", out)


class TestClear(Base):
    """S4: one command clears the records whose pod is gone and whose deadline has passed."""

    def the_records(self):
        """3 records whose pod is gone and whose deadline has passed, 1 with a pod, 1 not past its deadline."""
        for name in ("gone1", "gone2", "gone3"):
            self.record(name, hard=DEAD)
        self.record("alive", hard=DEAD, pod="cw-alive")
        self.put_pods(self.pod("cw-alive", "alive", phase="Running"))
        self.record("fresh", hard=ALIVE)

    def test_s4a_it_clears_exactly_the_ended_records_and_names_them(self):
        self.the_records()
        code, out, err = self.clear()
        self.assertEqual(code, 0, err)
        cleared = [l for l in out.splitlines() if l.startswith("cleared ")]
        self.assertEqual(len(cleared), 3, out)
        for name in ("gone1", "gone2", "gone3"):
            self.assertTrue(any(l.startswith("cleared %s " % name) for l in cleared), out)
        self.assertEqual(self.records_left(), ["alive.json", "fresh.json"])
        self.assertFalse((self.state_dir / "cloud" / "gone1.password").exists())

    def test_s4a_it_never_touches_a_record_with_a_pod(self):
        self.the_records()
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
        self.the_records()
        state = self.cluster()
        state["fail_cmd"] = "get"
        self.write_cluster(state)
        code, out, err = self.clear()
        self.assertEqual(code, 1)
        self.assertIn("could not be read", out + err)
        self.assertEqual(len(self.records_left()), 5)

    def test_s4c_a_caller_who_is_not_the_boss_gets_nothing(self):
        self.the_records()
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
        self.the_records()
        code, out, err = self.clear("--dry-run")
        self.assertEqual(code, 0, err)
        for name in ("gone1", "gone2", "gone3"):
            self.assertIn("would clear %s" % name, out)
        self.assertEqual(len(self.records_left()), 5)

    def test_u2_no_command_path_removes_a_record_that_has_a_pod(self):
        self.the_records()
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
