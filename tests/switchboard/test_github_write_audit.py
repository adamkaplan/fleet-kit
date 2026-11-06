"""Every GitHub write of the switchboard writes one audit line through one helper, at the write (ask #2260).

HOST is bin/test-switchboard (set by its loader line). The guard finds GitHub writes in the sources by what they do (the argv shapes of a write), never by a count.
"""
import ast
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
HOST = None   # set by bin/test-switchboard
SECRET = "ghp_FAKE_SECRET_0123456789"
FLAGS = {"--method", "-X", "--input", "-f", "-F", "--field", "--raw-field"}
WRITE_WORD_PAIRS = {("pr", v) for v in ("comment", "edit", "create", "close", "ready", "merge", "reopen")} | {("issue", v) for v in ("comment", "edit", "create", "close", "reopen")} | {("label", v) for v in ("create", "edit", "delete")}
GUARD_NAMES = ("github_write", "github_audited")


def argv_words(node):
    """The constant words of a list or tuple literal, or of `'...'.split()` on a string literal (None for any other node)."""
    if isinstance(node, ast.Tuple):   # a tuple of flag names is no argv: only one that names the gh itself
        words = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        return words if "gh" in words else None
    if isinstance(node, ast.List):
        return [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "split" and isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str):
        words = node.func.value.value.split()
        return words if "gh" in words else None
    return None


def write_shaped(node):
    """True for a list or tuple literal, or a `'...'.split()`, that is the argv (or a part of it) of a GitHub write."""
    words = argv_words(node)
    if "graphql" in words:   # a fixed read query (fields are its variables)
        return False
    if "api" not in words:   # `-f`, `-F` and `--field` are a write only to `gh api`; the long flags always
        words_flags = FLAGS - {"-f", "-F", "--field", "--raw-field"}
    else:
        words_flags = FLAGS
    if any(w in words_flags or w.startswith(("--method=", "--field=", "--raw-field=", "--input=")) or (w.startswith("-X") and len(w) > 2) for w in words):
        return True
    return any((a, b) in WRITE_WORD_PAIRS for a, b in zip(words, words[1:]))


def call_name(node):
    func = node.func if isinstance(node, ast.Call) else node
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def audited_by(expr):
    return isinstance(expr, ast.Call) and call_name(expr) in GUARD_NAMES


def gate_closure(sources):
    """The set of files whose `gh([...])` calls are audited at the closure: the file that defines `merge_gate_gh` holding a `gh` decorated with `github_audited` (the merge gate's runner of every call it makes),
    and bin/merge_gate.py, whose functions receive that runner as their `gh` parameter. A `gh(...)` call in any other file is not audited by it."""
    for name, text in sources.items():
        for fn in ast.walk(ast.parse(text)):
            if isinstance(fn, ast.FunctionDef) and fn.name == "merge_gate_gh":
                for node in ast.walk(fn):
                    if isinstance(node, ast.FunctionDef) and node.name == "gh" and any(audited_by(d) for d in node.decorator_list):
                        return {name, "bin/merge_gate.py"}
    return set()


def unaudited_writes(sources):
    """['file:line'] of every write-shaped argv that is neither inside a `with github_write(...)` nor in a function decorated with `github_audited`."""
    found = []
    closure = gate_closure(sources)
    for name, text in sources.items():
        tree = ast.parse(text)

        def walk(node, ok):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ok = ok or any(audited_by(d) for d in node.decorator_list)
            if isinstance(node, ast.With):
                ok = ok or any(audited_by(i.context_expr) for i in node.items)
            if isinstance(node, ast.Call) and name in closure and call_name(node) == "gh":
                ok = True
            if isinstance(node, (ast.List, ast.Tuple, ast.Call)) and not ok and argv_words(node) is not None and write_shaped(node):
                found.append("%s:%d" % (name, node.lineno))
            for child in ast.iter_child_nodes(node):
                walk(child, ok)
        walk(tree, False)
    return found


def real_sources():
    out = {}
    for path in [REPO / "bin" / "fleet-switchboard"] + sorted((REPO / "bin").glob("*.py")) + sorted((REPO / "bin" / "switchboard").glob("*.py")):
        if path.name.startswith(("cloud_", "test_")):
            continue
        out[str(path.relative_to(REPO))] = path.read_text()
    return out


def done(code=0, out="{}", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class Broken:
    """An audit log that cannot be written."""
    def append(self, *a, **k):
        raise OSError("disk full")


class TestGithubWriteAudit(unittest.TestCase):
    def setUp(self):
        self.sb = HOST.sb
        self.dir = Path(tempfile.mkdtemp(prefix="ghaudit-"))
        self.audit = self.sb.AuditLog(self.dir / "audit.jsonl")
        self.config = {"gh": "gh", "call_timeout_seconds": 5}
        self.engine = types.SimpleNamespace(config=self.config, audit=self.audit, actor="daemon", accounts=None)
        self.calls = []

    def runner(self, argv, **kw):
        self.calls.append((argv, kw))
        return done()

    def lines(self):
        return [json.loads(l) for l in (self.dir / "audit.jsonl").read_text().splitlines()] if (self.dir / "audit.jsonl").exists() else []

    def assertAudited(self, verb, endpoint, repo, number):
        """one intent line naming actor, verb, endpoint, repository, number, and one result line"""
        mine = [l for l in self.lines() if l["event"] == "github.write"]
        self.assertEqual([l["phase"] for l in mine], ["intent", "result"])
        intent = mine[0]
        self.assertEqual((intent["actor"], intent["detail"]["verb"], intent["detail"]["endpoint"], intent["detail"]["repo"], intent["detail"]["number"]),
                         ("daemon", verb, endpoint, repo, number))
        self.assertIs(mine[1]["ok"], True)
        self.assertNotIn(SECRET, json.dumps(self.lines()))

    def test_gh_write_writes_its_line_and_never_the_body(self):
        self.sb._gh_write(self.engine, self.runner, "acme/app", "repos/acme/app/issues/5/comments", "POST", {"body": SECRET})
        self.assertAudited("POST", "repos/acme/app/issues/5/comments", "acme/app", 5)
        self.assertEqual(len(self.calls), 1)

    def test_a_read_writes_no_line(self):
        self.sb._gh_write(self.engine, self.runner, "acme/app", "repos/acme/app/issues/5", "GET")
        self.assertEqual(self.lines(), [])

    def test_review_api_post_and_put_write_their_lines(self):
        for method in ("POST", "PUT"):
            self.audit = self.sb.AuditLog(self.dir / ("%s.jsonl" % method))
            self.engine.audit = self.audit
            self.runner_calls = 0
            ok, _, _ = self.sb._review_api(self.engine, self.runner, lambda: 0, "acme/app", "repos/acme/app/pulls/7/reviews", method, {"body": SECRET})
            self.assertTrue(ok)
            mine = [json.loads(l) for l in (self.dir / ("%s.jsonl" % method)).read_text().splitlines() if "github.write" in l]
            self.assertEqual([(l["phase"], l["detail"] and l["detail"].get("verb")) for l in mine], [("intent", method), ("result", None)])
            self.assertEqual(mine[0]["subject"], "acme/app#7")
            self.assertNotIn(SECRET, json.dumps(mine))

    def test_an_audit_that_cannot_be_written_means_no_write(self):
        self.engine.audit = Broken()
        with self.assertRaises(self.sb.IntentError):
            self.sb._gh_write(self.engine, self.runner, "acme/app", "repos/acme/app/issues/5/comments", "POST", {"body": "x"})
        ok, _, why = self.sb._review_api(self.engine, self.runner, lambda: 0, "acme/app", "repos/acme/app/pulls/7/reviews", "POST", {"body": "x"})
        self.assertFalse(ok)
        self.engine.audit = None   # a read-only switchboard
        with self.assertRaises(self.sb.IntentError):
            self.sb._gh_write(self.engine, self.runner, "acme/app", "repos/acme/app/issues/5", "PATCH", {"body": "x"})
        self.assertEqual(self.calls, [], "the count of writes made without their audit line is 0")

    def test_a_failed_write_leaves_a_failed_result(self):
        failing = lambda argv, **kw: done(1, "", "boom")
        with self.assertRaises(self.sb.IntentError):
            self.sb._gh_write(self.engine, failing, "acme/app", "repos/acme/app/issues/5/comments", "POST", {"body": "x"})
        mine = [l for l in self.lines() if l["event"] == "github.write"]
        self.assertEqual([(l["phase"], l.get("ok")) for l in mine], [("intent", None), ("result", False)])

    def test_the_other_paths_write_their_lines(self):
        reader = types.SimpleNamespace(gh="gh", runner=self.runner, timeout=5, accounts=None, repo="acme/app", env_for=lambda repo: None, invalidate=lambda *a: None)
        # gh_call (the plan receipt) and write_issue_body (orders), each with the audit of the state directory
        state = self.audit
        patch = mock.patch.object(self.sb, "github_state_audit", lambda: state)
        patch.start(); self.addCleanup(patch.stop)
        self.sb.gh_call(reader, "acme/app", "repos/acme/app/issues/5/comments", "POST", {"body": SECRET})
        self.sb.write_issue_body(reader, "acme/app", 6, SECRET)
        mine = [l for l in self.lines() if l["event"] == "github.write" and l["phase"] == "intent"]
        self.assertEqual([(l["detail"]["verb"], l["detail"]["number"]) for l in mine], [("POST", 5), ("PATCH", 6)])
        self.assertNotIn(SECRET, json.dumps(self.lines()))
        # gh_call refuses when the audit cannot be written
        patch2 = mock.patch.object(self.sb, "github_state_audit", lambda: Broken())
        patch2.start(); self.addCleanup(patch2.stop)
        before = len(self.calls)
        with self.assertRaises(self.sb.CommentEditFailed):
            self.sb.gh_call(reader, "acme/app", "repos/acme/app/issues/5/comments", "POST", {"body": "x"})
        self.assertEqual(len(self.calls), before)

    def test_merge_gate_writes_are_audited_and_reads_are_not(self):
        sb = self.sb
        patch = mock.patch.object(sb, "github_state_audit", lambda: self.audit)
        patch.start(); self.addCleanup(patch.stop)
        config = dict(sb.load_config() if False else self.config, gh_by_name="gh")
        with mock.patch.object(sb, "GhAccounts", lambda *a, **k: types.SimpleNamespace(env_for=lambda repo: None, scrub=lambda t: t)), mock.patch.object(sb, "refuse_excluded", lambda *a: None):
            gh = sb.merge_gate_gh(config, self.runner, "acme/app")
            gh(["pr", "view", "9", "--repo", "acme/app"])
            self.assertEqual(self.lines(), [])
            gh(["api", "-X", "PATCH", "repos/acme/app/pulls/9", "-f", "state=closed"])
            gh(["pr", "merge", "9", "-R", "acme/app"])
        mine = [l for l in self.lines() if l["event"] == "github.write" and l["phase"] == "intent"]
        self.assertEqual([(l["detail"]["verb"], l["detail"]["endpoint"], l["detail"]["number"]) for l in mine],
                         [("PATCH", "repos/acme/app/pulls/9", 9), ("MERGE", "pr merge", 9)])
        self.assertEqual(len(self.calls), 3)
        patch3 = mock.patch.object(sb, "github_state_audit", lambda: Broken()); patch3.start(); self.addCleanup(patch3.stop)
        with mock.patch.object(sb, "GhAccounts", lambda *a, **k: types.SimpleNamespace(env_for=lambda repo: None, scrub=lambda t: t)), mock.patch.object(sb, "refuse_excluded", lambda *a: None):
            gh = sb.merge_gate_gh(config, self.runner, "acme/app")
            code, _, why = gh(["pr", "merge", "9", "-R", "acme/app"])
        self.assertEqual((code, len(self.calls)), (1, 3))


class TestGithubWriteGuard(unittest.TestCase):
    def test_the_real_sources_have_no_unaudited_write(self):
        self.assertEqual(unaudited_writes(real_sources()), [])

    def test_a_planted_write_is_named(self):
        planted = "def sneaky(runner, gh, path):\n    return runner([gh, 'api', path, '--method', 'POST', '--input', '-'])\n"
        sources = dict(real_sources(), **{"bin/switchboard/planted.py": planted})
        self.assertEqual(unaudited_writes(sources), ["bin/switchboard/planted.py:2"])

    def test_a_planted_pr_merge_is_named_and_an_audited_one_is_not(self):
        bad = "def f(run):\n    run(['gh', 'pr', 'merge', '9'])\n"
        good = "def f(run):\n    with github_write(None, 'x', 'MERGE', 'pr merge', 'r', 9):\n        run(['gh', 'pr', 'merge', '9'])\n"
        self.assertEqual(unaudited_writes({"a.py": bad}), ["a.py:2"])
        self.assertEqual(unaudited_writes({"b.py": good}), [])

    def test_the_guard_does_not_count_sites(self):
        two = "def f(run):\n    run(['gh', 'api', 'x', '-X', 'PUT'])\n    run(['gh', 'api', 'y', '-X', 'PUT'])\n"
        self.assertEqual(unaudited_writes({"a.py": two}), ["a.py:2", "a.py:3"])

    def test_a_gh_call_outside_the_gate_is_not_exempt(self):
        for plant in ("def f(gh):\n    gh(['pr', 'merge', '9'])\n", "def f(gh):\n    gh(['api', '-X', 'PATCH', 'repos/a/b/pulls/1'])\n"):
            sources = dict(real_sources(), **{"bin/switchboard/planted.py": plant})
            self.assertEqual(unaudited_writes(sources), ["bin/switchboard/planted.py:2"])

    def test_a_tuple_and_a_split_argv_are_named(self):
        for plant in ("def f(run):\n    run(('gh', 'api', 'x', '--method', 'PUT'))\n", "def f(run):\n    run('gh pr merge 9'.split())\n", "def f(run):\n    run(['gh', 'api', 'x', '-XPATCH'])\n"):
            self.assertEqual(unaudited_writes({"a.py": plant}), ["a.py:2"])


if __name__ == "__main__":
    unittest.main()
