#!/usr/bin/env python3
"""Offline tests: the split of bin/cloud_worker.py into bin/cloud_worker.py and bin/cloud_cluster.py (ask #1647).

S2 the line counts, S3 every moved name is reachable on the module, S6 each patched name takes effect, S10 the old text equals the moved text with
the reads of names that stayed behind masked, S4 the fixed command set on the fake cluster gives identical outputs and call logs on the old file
(the frozen fixture) and on the new one. Run directly: python3 cloud/launcher/tests/test_cloud_split.py
"""
import ast
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import tokenize
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("FL" + "EET_SWITCHBOARD_UNDER_TEST", "1")
_loader = importlib.machinery.SourceFileLoader("cw_split_test_cloud_worker_mod", str(ROOT / "bin" / "test-cloud-worker"))
_spec = importlib.util.spec_from_loader("cw_split_test_cloud_worker_mod", _loader)
tcw = importlib.util.module_from_spec(_spec)
sys.modules["cw_split_test_cloud_worker_mod"] = tcw
_loader.exec_module(tcw)

BEFORE = ROOT / "tests" / "switchboard" / "fixtures" / "cloud_worker.before-1647"
STAYED = {"CloudError", "DEFAULT_LAUNCHER", "EXIT_REASONS", "H", "LAUNCHER_ENV", "LAUNCH_SECONDS", "PASSWORD_MAX", "_OWN_BIN", "_one", "_read_private",
          "Refusal", "__file__"}
FIRST_MOVED, LAST_MOVED = "def checkout_launcher_path():", "        if child in cluster.tunnels:\n            cluster.tunnels.remove(child)"


def top_names(source):
    names = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names += [n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)]
    return names


def unmask(text):
    """The moved text with `D["name"]` put back to `name` (the reads of names that stayed behind)."""
    return re.sub(r'D\["(%s)"\]' % "|".join(sorted(STAYED)), r"\1", text)


def old_ranges():
    """The two ranges of the old file that moved: from the first function of the launcher process up to Refusal, and from the vault text to the tunnel."""
    lines = BEFORE.read_text().split("\n")
    first = lines.index(FIRST_MOVED)
    refusal = lines.index("class Refusal(CloudError):")
    vault = next(i for i, l in enumerate(lines) if l.startswith("VAULT_CHECK = ("))
    last = next(i for i, l in enumerate(lines) if l.startswith("def refusals(function):"))
    return [l for l in lines[first:refusal - 2]], [l for l in lines[vault:last - 2]]


class TestSplitSize(unittest.TestCase):
    def test_s2_line_counts(self):
        worker = len((ROOT / "bin" / "cloud_worker.py").read_text().split("\n")) - 1
        cluster = len((ROOT / "bin" / "cloud_cluster.py").read_text().split("\n")) - 1
        print("S2: bin/cloud_worker.py %d lines, bin/cloud_cluster.py %d lines" % (worker, cluster))
        self.assertLessEqual(worker, 1360)
        self.assertLessEqual(cluster, 1500)


class TestSplitNames(unittest.TestCase):
    def load(self):
        loader = importlib.machinery.SourceFileLoader("cw_split_names_mod", str(ROOT / "bin" / "cloud_worker.py"))
        spec = importlib.util.spec_from_loader("cw_split_names_mod", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module

    def test_s3_every_moved_name_is_reachable(self):
        module = self.load()
        moved = [n for n in top_names((ROOT / "bin" / "cloud_cluster.py").read_text()) if n != "D"]
        missing = [n for n in moved if not hasattr(module, n)]
        print("S3: moved names %d, missing %d" % (len(moved), len(missing)))
        self.assertEqual(missing, [])
        self.assertIs(module.Cluster, module.cloud_cluster.Cluster)

    def test_s10_the_moved_text_equals_the_old_text_with_the_reads_masked(self):
        first, second = old_ranges()
        new = unmask((ROOT / "bin" / "cloud_cluster.py").read_text()).split("\n")
        start = new.index(FIRST_MOVED)
        old = first + ["", ""] + second
        got = [l for l in new[start:] if True]
        while got and got[-1] == "":
            got.pop()
        while old and old[-1] == "":
            old.pop()
        differing = sum(1 for a, b in zip(old, got) if a != b) + abs(len(old) - len(got))
        print("S10: differing lines %d of %d" % (differing, len(old)))
        self.assertEqual(differing, 0)


class TestSplitPatches(tcw.Base):
    """Each name that a pin patches: patched on the module that holds it, the command that uses it feels the patch."""

    def test_s6_patched_names_take_effect(self):
        cw = tcw.sb.load_module("cloud_worker").bind(tcw.sb.__dict__)
        cluster = cw.cloud_cluster
        seen = []
        shown = 0
        # a name that stayed behind, patched on bin/cloud_worker.py, reaches the moved code
        with mock.patch.object(cw, "LAUNCHER_ENV", "FL" + "EET_SPLIT_TEST_ENV"):
            with mock.patch.dict(os.environ, {"FL" + "EET_SPLIT_TEST_ENV": "/opt/split/launcher"}):
                seen.append(cluster.launcher_argv() == ["/opt/split/launcher"])
        for name, value in (("checkout_launcher_path", lambda: Path("/split/entry")), ("LAUNCHER_OUTPUT_LIMIT", 7), ("VAULT_LOCK_GRACE", 0),
                            ("_capture", lambda *a, **k: (0, "split", ""))):
            with mock.patch.object(cluster, name, value):
                seen.append(getattr(cluster, name) is value)
            seen.append(getattr(cluster, name) is not value)
        with mock.patch.object(cluster, "checkout_launcher_path", lambda: Path("/split/entry")), mock.patch.dict(os.environ, {"FL" + "EET_CLOUD_WORKER_BIN": ""}):
            try:
                cluster.launcher_argv()
            except cw.Refusal as err:
                seen.append("/split/entry" in str(err))
        with mock.patch.object(cluster, "_capture", lambda *a, **k: (0, "split-ran", "")):
            seen.append(cluster._capture(["x"]) == (0, "split-ran", ""))
        with mock.patch.object(cluster, "_ps", lambda pid, field: "split-ps"):
            seen.append(cluster._ps(1, "lstart") == "split-ps")
        shown = sum(1 for s in seen if s)
        print("S6: patched names %d of %d take effect" % (shown, len(seen)))
        self.assertEqual(shown, len(seen))


class TestSplitCommands(tcw.VaultBase):
    """S4: one scripted set of commands on the fake cluster, on the old file (the frozen fixture) and on the new one."""

    def old_module(self):
        tree = Path(tempfile.mkdtemp(prefix="cwsplit-"))
        self.addCleanup(shutil.rmtree, str(tree), True)
        (tree / "bin").mkdir()
        shutil.copy(str(BEFORE), str(tree / "bin" / "cloud_worker.py"))
        for name in ("path_walk.py",):
            shutil.copy(str(ROOT / "bin" / name), str(tree / "bin" / name))
        os.symlink(str(ROOT / "cloud"), str(tree / "cloud"))
        loader = importlib.machinery.SourceFileLoader("cw_before_1647", str(tree / "bin" / "cloud_worker.py"))
        spec = importlib.util.spec_from_loader("cw_before_1647", loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["cw_before_1647"] = module
        self.addCleanup(sys.modules.pop, "cw_before_1647", None)
        loader.exec_module(module)
        return module

    def normal(self, text):
        text = text.replace(str(self.home), "<home>")
        text = re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:<port>", text)
        text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", "<time>", text)
        text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<id>", text)
        text = re.sub(r"cw-[0-9a-f]{6,}", "cw-<id>", text)
        text = re.sub(r"=\d+\b", "=<n>", text)
        text = re.sub(r"\b[0-9a-f]{32}\b", "<run-id>", text)
        text = re.sub(r"cmd-\d+-[A-Za-z0-9_]+", "cmd-<n>", text)
        text = re.sub(r"\b\d{4,5}:4096", "<port>:4096", text)
        text = re.sub(r"\b(fd|pid) \d+", r"\1 <n>", text)
        return text

    def reset_vault(self):
        for stale in (self.checkout / "runs.jsonl", self.checkout / "fixed", self.stub_dir / "az.log"):
            stale.unlink(missing_ok=True)

    def no_id_answers(self):
        """A patch of the fake pod's answer writer: a prompt answer carries no message id while it is active."""
        handler = self.remote.server.RequestHandlerClass
        real = handler._answer

        def answer(this, status, body):
            if this.path.endswith("/prompt") and isinstance(body, dict) and isinstance(body.get("data"), dict):
                body = {"data": {k: v for k, v in body["data"].items() if k != "id"}}
            return real(this, status, body)
        return mock.patch.object(handler, "_answer", answer)

    def script(self):
        out = []

        def run(label, *argv, fn=None):
            code, so, se = fn() if fn else self.main(*argv)
            out.append("%s: exit %d" % (label, code))
            lines = self.normal(so).splitlines()
            if argv == ("status",):
                lines = [l for l in lines if l.strip().startswith("cloud")]   # the host lines (load, memory, disk) differ from run to run
            out.extend("%s out: %s" % (label, l) for l in lines)
            out.extend("%s err: %s" % (label, l) for l in self.normal(se).splitlines() if "Warning" not in l and not l.startswith("  "))

        self.reset_vault()
        self.make_checkout(code=10, fixes=True)
        run("launch", fn=lambda: self.launch("coder7"))
        run("launch --stack", fn=lambda: self.launch_with("--cloud", "--stack", "--shape", "S", name="coder8"))
        run("status", "status")
        run("send", "send", "coder7", "please", "rebase")
        with self.no_id_answers():
            run("send without id", "send", "coder7", "second", "note")
        run("read", "read", "coder7")
        run("retire", "retire", "coder7", "--reason", "done")
        run("refused launch", fn=lambda: self.launch_with("--cloud", repo="other-org/other-repo", name="coder9"))
        self.reset_vault()
        self.make_checkout(code=40, fixes=False)
        run("vault refusal", fn=lambda: self.launch("coder10"))
        run("cloud-clear", "cloud-clear")
        calls = [self.normal(json.dumps(c)) for c in self.stub_calls()]
        kube = [self.normal(json.dumps(c)) for c in self.jsonl("kubectl.log")]
        az = [self.normal(l) for l in ((self.stub_dir / "az.log").read_text().splitlines() if (self.stub_dir / "az.log").exists() else [])]
        return out, calls, kube, az

    def mutant_module(self):
        tree = Path(tempfile.mkdtemp(prefix="cwsplit-mut-"))
        self.addCleanup(shutil.rmtree, str(tree), True)
        (tree / "bin").mkdir()
        text = (ROOT / "bin" / "cloud_worker.py").read_text()
        before = text.count('print(message_id or "sent")')
        text = text.replace('print(message_id or "sent")', 'print(message_id or "sent!")')
        self.applied = before == 1 and text.count('print(message_id or "sent!")') == 1
        (tree / "bin" / "cloud_worker.py").write_text(text)
        for name in ("cloud_cluster.py", "path_walk.py"):
            shutil.copy(str(ROOT / "bin" / name), str(tree / "bin" / name))
        os.symlink(str(ROOT / "cloud"), str(tree / "cloud"))
        loader = importlib.machinery.SourceFileLoader("cw_mutant_1647", str(tree / "bin" / "cloud_worker.py"))
        spec = importlib.util.spec_from_loader("cw_mutant_1647", loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["cw_mutant_1647"] = module
        self.addCleanup(sys.modules.pop, "cw_mutant_1647", None)
        loader.exec_module(module)
        return module

    def test_s4_mutant_the_changed_fallback_text_is_seen(self):
        new = self.script()
        module = self.mutant_module()
        real = tcw.sb.load_module
        self.addCleanup(setattr, tcw.sb, "load_module", real)
        tcw.sb.load_module = lambda name: module if name == "cloud_worker" else real(name)
        for path in (self.state, self.stub_dir / "calls.log", self.stub_dir / "az.log", self.stub_dir / "kubectl.log"):
            shutil.rmtree(str(path), True) if path.is_dir() else path.unlink(missing_ok=True)
        for fresh in (self.remote.requests, self.remote.bodies, self.remote.messages):
            del fresh[:]
        module.bind(tcw.sb.__dict__)
        lib = module._lib()
        for name in ("PREFLIGHT_ARGV", "VAULT_CHECK_PYTHON"):
            hook = mock.patch.object(lib, name, getattr(self.lib, name))
            hook.start()
            self.addCleanup(hook.stop)
        mutant = self.script()
        differing = sum(1 for a, b in zip(mutant[0], new[0]) if a != b) + abs(len(mutant[0]) - len(new[0]))
        print("S4 mutant: differing lines %d (at least 1), mutation applied %s" % (differing, self.applied))
        self.assertTrue(self.applied)
        self.assertGreaterEqual(differing, 1)

    def test_s4_the_scripted_commands_give_the_same_outputs_and_call_logs(self):
        new = self.script()
        old_module = self.old_module()
        real = tcw.sb.load_module
        self.addCleanup(setattr, tcw.sb, "load_module", real)
        tcw.sb.load_module = lambda name: old_module if name == "cloud_worker" else real(name)
        for path in (self.state, self.stub_dir / "calls.log", self.stub_dir / "az.log", self.stub_dir / "kubectl.log"):
            shutil.rmtree(str(path), True) if path.is_dir() else path.unlink(missing_ok=True)
        for fresh in (self.remote.requests, self.remote.bodies, self.remote.messages):
            del fresh[:]
        old_module.bind(tcw.sb.__dict__)
        old_lib = old_module._lib()   # the same stand-ins the new file's launcher library got in setUp
        for name in ("PREFLIGHT_ARGV", "VAULT_CHECK_PYTHON"):
            hook = mock.patch.object(old_lib, name, getattr(self.lib, name))
            hook.start()
            self.addCleanup(hook.stop)
        old = self.script()
        outputs = sum(1 for a, b in zip(old[0], new[0]) if a != b) + abs(len(old[0]) - len(new[0]))
        logs = sum(sum(1 for a, b in zip(x, y) if a != b) + abs(len(x) - len(y)) for x, y in zip(old[1:], new[1:]))
        print("S4: differing lines %d of %d in the outputs, %d in the call logs" % (outputs, len(new[0]), logs))
        self.assertTrue(any(l.startswith("vault refusal: exit 2") for l in new[0]), new[0][-8:])
        if outputs: print("\n".join("%r | %r" % (a, b) for a, b in [p for p in zip(old[0], new[0]) if p[0] != p[1]][:8]))
        for x, y in zip(old[1:], new[1:]):
            if logs:
                print("\n".join("%.230s\n  %.230s" % p for p in zip(x, y) if p[0] != p[1]))
        self.assertEqual(outputs, 0)
        self.assertEqual(logs, 0)


if __name__ == "__main__":
    unittest.main()
