import ast
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(WORKER))
POD = os.path.join(WORKER, "cw-plan-check")
SWITCHBOARD = os.path.join(ROOT, "bin", "fl" + "eet-switchboard")
DONE = "the thing works"


def plan(pass_rule="exit status is 0 and stdout has 3 lines", size="size: M (two slices, agent time)", cover="DW-1", discr="a stub that prints nothing fails the 3 line check"):
    return """## Plan (scenarios)

plan-format: 2
%s
unchanged: none because the unchanged behaviours are covered elsewhere here

```yaml
clauses:
  DW-1: the thing works
```

```yaml
scenarios:
  - id: S1
    covers: %s
    text: Given a file, when it runs, then it prints
    proof: test
    pass: %s
    discriminates: %s
    evaluator: ci
```
""" % (size, cover, pass_rule, discr)


def read(p):
    with open(p, "rb") as f:
        return f.read()


class PlanCheckInPod(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, text):
        p = os.path.join(self.tmp.name, "plan.md")
        with open(p, "w") as f:
            f.write(text)
        return p

    def pod(self, path, extra=()):
        return subprocess.run([sys.executable, POD, "--file", path, "--done-when", DONE] + list(extra), capture_output=True, text=True)

    def local(self, path):
        return subprocess.run([sys.executable, SWITCHBOARD, "plan", "check", "--file", path, "--done-when", DONE], capture_output=True, text=True)

    def test_s1_docs_section(self):
        text = read(os.path.join(ROOT, "docs", "cloud-worker-interface.md")).decode()
        head = "## Why a cloud worker's plan check and name were stuck"
        self.assertEqual(text.count(head), 1)
        sec = text.split(head)[1].split("\n## ")[0]
        self.assertIn("no switchboard command in the pod", sec)
        self.assertIn("name record kept after an operator delete", sec)

    def test_s2_vague_pass_rule_refused_with_local_line(self):
        p = self.write(plan(pass_rule="works"))
        pod, loc = self.pod(p), self.local(p)
        self.assertEqual(pod.returncode, 1)
        self.assertTrue(pod.stdout.split("\n")[0].endswith("FAIL"), pod.stdout)
        self.assertEqual(pod.stdout, loc.stdout)
        self.assertEqual(pod.returncode, loc.returncode)

    def test_s3_sound_plan_passes_like_local(self):
        p = self.write(plan())
        pod, loc = self.pod(p), self.local(p)
        self.assertEqual(pod.returncode, 0, pod.stdout)
        self.assertTrue(pod.stdout.split("\n")[0].endswith("PASS"))
        self.assertEqual(pod.stdout, loc.stdout)

    def test_s4_fixtures_agree_and_copy_is_identical(self):
        self.assertEqual(read(os.path.join(WORKER, "plan_check.py")), read(os.path.join(ROOT, "bin", "plan_check.py")))
        fixtures = [
            plan(), plan(pass_rule="works"), plan(pass_rule="it is good"), plan(pass_rule="ok"),
            plan(discr="it fails"), plan(discr=""), plan(size=""), plan(size="size: M (3 days)"),
            plan(size="size: S (2 weeks)"), plan(cover="DW-9"), plan(cover=""), plan(pass_rule=""),
            "no plan here", "```yaml\nclauses: [\n```\n", plan().replace("DW-1: the thing works", "DW-1: other"),
        ]
        self.assertGreaterEqual(len(fixtures), 12)
        for i, fx in enumerate(fixtures):
            p = self.write(fx)
            pod, loc = self.pod(p), self.local(p)
            self.assertEqual((pod.stdout, pod.returncode), (loc.stdout, loc.returncode), i)

    def test_unknown_argument_and_unreadable(self):
        p = self.write(plan())
        r = self.pod(p, ["--ask", "x"])
        self.assertEqual(r.returncode, 2)
        r = self.pod(os.path.join(self.tmp.name, "missing"))
        self.assertEqual(r.returncode, 2)
        r = subprocess.run([sys.executable, POD, "--file", p], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    def test_s6_pure_read(self):
        p = self.write(plan())
        before = sorted(os.listdir(self.tmp.name)), read(p)
        tree = ast.parse(read(POD).decode())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name for a in n.names}
        self.assertEqual(mods - {"json"}, {"sys", "os", "re", "argparse", "plan_check"})
        guard = ("import socket,sys,runpy\n"
                 "def boom(*a, **k): raise RuntimeError('network')\n"
                 "socket.socket.connect = boom; socket.create_connection = boom; socket.getaddrinfo = boom\n"
                 "sys.argv = ['cw-plan-check', '--file', %r, '--done-when', %r]\n"
                 "runpy.run_path(%r, run_name='__main__')\n" % (p, DONE, POD))
        os.chmod(self.tmp.name, 0o555)
        self.addCleanup(os.chmod, self.tmp.name, 0o755)
        r = subprocess.run([sys.executable, "-c", guard], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((sorted(os.listdir(self.tmp.name)), read(p)), before)

    def test_u1_guard_and_permissions_untouched(self):
        # Pinned hashes of the files at the time of this change (they must not change for the plan check).
        self.assertEqual(hashlib.sha256(read(os.path.join(WORKER, "guard.sh"))).hexdigest(), PINNED["guard.sh"])
        # opencode-worker.json: a readable pin. The rows that existed before ask 1083 are pinned as one digest of their canonical JSON;
        # every row an ask adds is listed in ADDED_ROWS below, so a later ask shows its rows as a plain diff line, not a new hash.
        import json
        with open(os.path.join(WORKER, "opencode-worker.json")) as f:
            conf = json.load(f)
        bash = conf["permission"]["bash"]
        self.assertEqual(bash, conf["agent"]["worker"]["permission"]["bash"])
        listed = {k for k, _ in ADDED_ROWS}
        self.assertEqual([(k, v) for k, v in bash.items() if k in listed], ADDED_ROWS)
        base = {k: v for k, v in bash.items() if k not in listed}
        rest = dict(conf, permission={"bash": base}, agent={"worker": dict(conf["agent"]["worker"], permission={"bash": base})})
        self.assertEqual(hashlib.sha256(json.dumps(rest, sort_keys=True).encode()).hexdigest(), PINNED_JSON_WITHOUT_ADDED_ROWS)
        self.assertNotIn(b"cw-plan-check", read(os.path.join(WORKER, "guard.sh")))
        self.assertNotIn(b"cw-plan-check", read(os.path.join(WORKER, "opencode-worker.json")))

    def test_m3_the_moved_deny_rows_come_after_every_added_allow_row(self):
        for keys in bash_key_lists(open(os.path.join(WORKER, "opencode-worker.json")).read()):
            self.assertEqual(len(keys), len(set(keys)), "a duplicate key")
            self.assertEqual(order_problems(keys), [])
            self.assertEqual([k for k in keys if k in MOVED_DENY_ORDER], MOVED_DENY_ORDER)

    def test_m3_a_mutant_that_moves_put_before_the_allows_is_seen(self):
        text = open(os.path.join(WORKER, "opencode-worker.json")).read()
        line = '      "gh api*PUT*": "deny",\n'
        self.assertIn(line, text)
        moved = text.replace(line, "", 1).replace('      "gh api*/issues/*/comments": "allow",\n', line + '      "gh api*/issues/*/comments": "allow",\n', 1)
        self.assertNotEqual(moved, text)
        self.assertNotEqual(order_problems(bash_key_lists(moved)[0]), [])

    def test_s11_docs_and_readme(self):
        for rel in ("cloud/README.md", "docs/cloud-worker-interface.md"):
            t = read(os.path.join(ROOT, rel)).decode()
            self.assertIn("cw-plan-check", t, rel)
            self.assertIn("a retire of a worker whose pod is already gone", t, rel)
        readme = read(os.path.join(ROOT, "cloud", "README.md")).decode()
        self.assertIn("`plan_check.py`", readme)

    def test_dockerfile_installs_both(self):
        d = read(os.path.join(WORKER, "Dockerfile")).decode()
        self.assertIn("COPY worker/cw-plan-check worker/plan_check.py /usr/local/bin/", d)

    def test_drift_pin_parse_intent(self):
        def pieces(path):
            src = read(path).decode()
            tree = ast.parse(src)
            out = {}
            for n in tree.body:
                if isinstance(n, ast.FunctionDef) and n.name == "parse_intent":
                    out["parse_intent"] = ast.get_source_segment(src, n).strip()
                if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in ("_HEADING", "_DONE_WHEN"):
                    out[n.targets[0].id] = ast.get_source_segment(src, n).strip()
            return out
        a, b = pieces(SWITCHBOARD), pieces(POD)
        self.assertEqual(sorted(a), ["_DONE_WHEN", "_HEADING", "parse_intent"])
        self.assertEqual(a, b)

    def test_parity_corpus(self):
        base = plan()
        intent = lambda dw: "## Intent\n\nMake it work.\n%s\n\n" % dw
        cases = {
            "no intent": base,
            "equal": intent("Done when: " + DONE) + base,
            "differs": intent("Done when: something else") + base,
            "no done when line": intent("") + base,
            "fenced": "```\n## Intent\nDone when: other\n```\n" + base,
            "crlf": (intent("Done when: other") + base).replace("\n", "\r\n"),
            "two intents": intent("Done when: other") + intent("Done when: third") + base,
            "list": intent("- Done when: other") + base,
            "bold": intent("**Done when:** other") + base,
            "other level": "### Intent\nx\nDone when: other\n" + base,
            "case": intent("DONE WHEN: other") + base,
            "empty": intent("Done when:") + base,
            "equal bold": intent("**Done when:** " + DONE) + base,
            "scenarios and differs": base + "\n" + intent("Done when: zzz"),
            "differs vague": intent("Done when: other") + plan(pass_rule="works"),
        }
        self.assertGreaterEqual(len(cases), 14)
        for name, text in cases.items():
            p = self.write(text)
            for extra in ((), ("--json",)):
                pod = self.pod(p, extra)
                loc = subprocess.run([sys.executable, SWITCHBOARD, "plan", "check", "--file", p, "--done-when", DONE] + list(extra), capture_output=True, text=True)
                self.assertEqual((pod.returncode, pod.stdout), (loc.returncode, loc.stdout), name)
        p = self.write(cases["differs"])
        r = self.pod(p)
        self.assertEqual(r.returncode, 1)
        self.assertIn("done-when-differs", r.stdout)

    def test_mutants_detected(self):
        src = read(POD).decode()
        p = self.write(intent_differs())
        def run(mut, p=p):
            return run_on(self, mut, p)
        def _unused(mut):
            q = os.path.join(self.tmp.name, "cw-mut")
            with open(q, "w") as f:
                f.write(mut)
            for n in ("plan_check.py",):
                d = os.path.join(self.tmp.name, n)
                if not os.path.exists(d):
                    with open(d, "wb") as f:
                        f.write(read(os.path.join(WORKER, n)))
            return subprocess.run([sys.executable, q, "--file", p, "--done-when", DONE], capture_output=True, text=True).returncode
        self.assertEqual(run(src), 1)
        self.assertEqual(run(src.replace("plan_own_done_when(text, args.done_when))", "None)")), 0)
        self.assertEqual(run(src.replace("done_when = done_when or match", "done_when = done_when or None; _x = match")), 0)
        # no != comparison: an Intent Done-when equal to --done-when would wrongly be reported as differing
        eq = self.write("## Intent\n\nx\nDone when: %s\n\n" % DONE + plan())
        self.assertEqual(run_on(self, src, eq), 0)
        # check() treats an equal plan_own as no defect, so the comparison is pinned in the source too
        self.assertIn("own if own and own != done_when else None", src)


def run_on(self, mut, p):
    q = os.path.join(self.tmp.name, "cw-mut")
    with open(q, "w") as f:
        f.write(mut)
    d = os.path.join(self.tmp.name, "plan_check.py")
    with open(d, "wb") as f:
        f.write(read(os.path.join(WORKER, "plan_check.py")))
    return subprocess.run([sys.executable, q, "--file", p, "--done-when", DONE], capture_output=True, text=True).returncode


def intent_differs():
    return "## Intent\n\nMake it work.\nDone when: something else\n\n" + plan()


def bash_key_lists(text):
    """The keys of both bash blocks IN FILE ORDER, duplicates kept (object_pairs_hook)."""
    import json
    found = []
    def hook(pairs):
        if any(k == "gh pr merge*" for k, _ in pairs):
            found.append([k for k, _ in pairs])
        return dict(pairs)
    json.loads(text, object_pairs_hook=hook)
    return found


def order_problems(keys):
    """opencode: the last matching rule wins, so each moved deny row must come AFTER every added allow row."""
    last_allow = max(i for i, k in enumerate(keys) if k in ADDED_ALLOWS)
    return [k for k in MOVED_DENY_ORDER if keys.index(k) < last_allow]


PINNED = {'guard.sh': 'ba4d51aa043d450a4832210fb6492295e7bd7c7e61a4abd2a70cc75baea19bca'}
PINNED_JSON_WITHOUT_ADDED_ROWS = "c14522cff99b9223d881b9f80a3d3397f55ae15a5d6f12a2380b89c9de6fd4be"
# Rows added to opencode-worker.json, in file order. A pin changes only in the ask whose approved plan names the rows:
# Lower and capitalised method words after -X or --method are denied too (the glob is case sensitive; a mixed form such as pOsT is not covered here, the client refuses it).
# asks 1166 and 1200 add their rows to THIS list.
ADDED_ROWS = [   # ask 1083: DW-6 (read an issue's comments and the two pull request read paths, GET only; write methods and flags stay denied)
    ('gh api*/issues/*/comments', 'allow'),
    ('gh api*/issues/*/comments *', 'allow'),
    ('gh api*/issues/*/comments?*', 'allow'),
    ('gh api*/pulls/*/reviews', 'allow'),
    ('gh api*/pulls/*/reviews *', 'allow'),
    ('gh api*/pulls/*/reviews?*', 'allow'),
    ('gh api*/pulls/*/comments', 'allow'),
    ('gh api*/pulls/*/comments *', 'allow'),
    ('gh api*/pulls/*/comments?*', 'allow'),
    ('gh api*POST*', 'deny'),
    ('gh api*-X post*', 'deny'),
    ('gh api*--method post*', 'deny'),
    ('gh api*-X Post*', 'deny'),
    ('gh api*--method Post*', 'deny'),
    ('gh api*-X patch*', 'deny'),
    ('gh api*--method patch*', 'deny'),
    ('gh api*-X Patch*', 'deny'),
    ('gh api*--method Patch*', 'deny'),
    ('gh api*-X put*', 'deny'),
    ('gh api*--method put*', 'deny'),
    ('gh api*-X Put*', 'deny'),
    ('gh api*--method Put*', 'deny'),
    ('gh api*-X delete*', 'deny'),
    ('gh api*--method delete*', 'deny'),
    ('gh api*-X Delete*', 'deny'),
    ('gh api*--method Delete*', 'deny'),
    ('gh api* -f*', 'deny'),
    ('gh api* -F*', 'deny'),
    ('gh api*--field*', 'deny'),
    ('gh api*--raw-field*', 'deny'),
    ('gh api*--input*', 'deny'),
    ('gh api*graphql*', 'deny'),
    ('gh api*mutation*', 'deny'),
    # ask 1200: DW-2, DW-3 (a worker edits the body of its own pull request: `pr edit N --body-file F` only; every other pr-edit form stays denied)
    ('gh pr edit*', 'deny'),
    ('gh pr edit * --body-file *', 'allow'),
    ('gh pr edit*--body-file* --*', 'deny'),
    ('gh pr edit*--* --body-file*', 'deny'),
    ('gh pr edit*://*', 'deny'),
    ('gh pr edit*--repo*', 'deny'),
    # ask 1200, S14: a shell command naming the pod's record of its own pull requests (the variable or the directory) is refused
    ('*CW_PR_RECORD*', 'deny'),
    ('*pr-record*', 'deny'),
]
ADDED_ALLOWS = [k for k, v in ADDED_ROWS if v == 'allow']
# The three deny rows that exist before ask 1083 and were moved: their order, and they come after every added allow row above.
MOVED_DENY_ORDER = ['gh api*PATCH*', 'gh api*PUT*', 'gh api*DELETE*']

if __name__ == "__main__":
    unittest.main()
