"""#1573: the preflight reads an organization ruleset at repository level (S2, S4, S5, S7, S10). Offline: its own fake GitHub on 127.0.0.1
that records every request path; the real preflight.py runs as a subprocess against it (nothing is imported from test_preflight.py)."""
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
REPO = "acme-org/widget-service"
MARKER = "synthetic" + "Marker" + "Zq7" * 4          # planted as the token of every read (assembled: no literal in the source)
ANSWER = "planted" + "Answer" + "Text" + "Kx9" * 3   # planted in the answers' own text
RULES = ["pull_request", "non_fast_forward", "deletion"]
ENTRY = {"approved_by": "the-owner", "approved_at": "2026-10-16", "reason": "protected by the org ruleset", "pr": "12"}


class Fake(http.server.BaseHTTPRequestHandler):
    detail = "ok"      # ok | 403 | 404 | 500 | repo (the same ruleset sourced from the repository, field absent) | bypass-less ok
    paths = []
    orgs = 0

    def log_message(self, *a):
        pass

    def reply(self, code, body):
        out = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        path = urlparse(self.path).path
        Fake.paths.append(path)
        if path.startswith("/orgs/"):
            Fake.orgs += 1
            return self.reply(403, {"message": ANSWER})
        if path == "/repos/" + REPO:
            return self.reply(200, {"default_branch": "main", "allow_auto_merge": False, "description": ANSWER})
        if path.startswith("/repos/%s/rules/branches/" % REPO):
            src = ("Repository", REPO) if Fake.detail == "repo" else ("Organization", "acme-org")
            out = []
            for t in RULES:
                r = {"type": t, "ruleset_id": 9, "ruleset_source_type": src[0], "ruleset_source": src[1]}
                if t == "pull_request":
                    r["parameters"] = {"required_approving_review_count": 1}
                out.append(r)
            return self.reply(200, out)
        if path == "/repos/%s/rulesets/9" % REPO:
            if Fake.detail in ("403", "404", "500"):
                return self.reply(int(Fake.detail), {"message": ANSWER})
            return self.reply(200, {"id": 9, "name": ANSWER, "target": "branch", "enforcement": "active",
                                    "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}}})
        if path.startswith("/repos/%s/branches/" % REPO):
            return self.reply(200, {"name": "main", "protected": False})
        self.reply(404, {"message": ANSWER})


def serve():
    srv = http.server.HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_port


def program_copy(root, name="launcher", preflight_text=None):
    """A private copy of the program (its repo-approvals.json is the only approvals file there is); returns the preflight path."""
    d = os.path.join(root, name)
    os.makedirs(d)
    for n in ("preflight.py", "app_token.py", "token_mint.py", "repo-approvals.json"):
        shutil.copy(os.path.join(LAUNCHER, n), d)
    if preflight_text is not None:
        with open(os.path.join(d, "preflight.py"), "w") as f:
            f.write(preflight_text)
    return os.path.join(d, "preflight.py")


def run(prog, base, detail):
    Fake.detail, Fake.paths, Fake.orgs = detail, [], 0
    env = dict(os.environ, CW_GITHUB_API_BASE=base, CW_PREFLIGHT_TOKEN_CMD="echo " + MARKER)
    p = subprocess.run([sys.executable, prog, REPO], env=env, capture_output=True, text=True, timeout=60)
    return p, list(Fake.paths), Fake.orgs


def s2(prog, base):
    p, paths, orgs = run(prog, base, "ok")
    named = sum(1 for x in paths if x == "/repos/%s/rulesets/9" % REPO)   # a pass line carries no ruleset id: the read of ruleset 9 is the naming
    nv = sum(1 for x in p.stdout.splitlines() if "NOT VERIFIED" in x)
    passing = 1 if p.returncode == 0 and "preflight ok" in p.stdout else 0
    return "S2: repositories passing %d of 1, ruleset 9 named %d line, NOT VERIFIED lines %d, requests under /orgs/ %d" % (passing, named, nv, orgs)


S2_LINE = "S2: repositories passing 1 of 1, ruleset 9 named 1 line, NOT VERIFIED lines 0, requests under /orgs/ 0"


def s5(prog, base, root):
    p403, _, _ = run(prog, base, "403")
    fail_lines = sum(1 for x in p403.stdout.splitlines() if " FAIL " in x)
    ok403 = p403.returncode == 1 and fail_lines == 1
    appr = program_copy(root, "approved" + str(len(os.listdir(root))), open(prog).read())
    with open(os.path.join(os.path.dirname(appr), "repo-approvals.json"), "w") as f:
        json.dump({hashlib.sha256(REPO.lower().encode()).hexdigest(): ENTRY}, f)
    pa, _, _ = run(appr, base, "403")
    nv = sum(1 for x in pa.stdout.splitlines() if "NOT VERIFIED" in x)
    okappr = pa.returncode == 0 and nv == 1
    p404, _, _ = run(prog, base, "404")
    p500, _, _ = run(prog, base, "500")
    ok2 = p404.returncode == 2 and p500.returncode == 2
    return ("S5: 403 at the repo-level detail: exit %d with %d FAIL line; with an owner approval entry: exit %d with %d NOT VERIFIED line; 404: exit %d; 500: exit %d"
            % (p403.returncode, fail_lines, pa.returncode, nv, p404.returncode, p500.returncode)), ok403 and okappr and ok2


S5_LINE = ("S5: 403 at the repo-level detail: exit 1 with 1 FAIL line; with an owner approval entry: exit 0 with 1 NOT VERIFIED line; 404: exit 2; 500: exit 2")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = serve()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prog = program_copy(self.tmp.name)


class S2Reads(Base):
    def test_S2_the_organization_ruleset_is_read_at_repository_level_and_never_under_orgs(self):
        line = s2(self.prog, self.base)
        print(line)
        self.assertEqual(line, S2_LINE)
        self.assertIn("/repos/%s/rulesets/9" % REPO, Fake.paths)


class S5FailsClosed(Base):
    def test_S5_403_fails_closed_the_approval_entry_works_and_404_and_500_are_exit_2(self):
        line, ok = s5(self.prog, self.base, self.tmp.name)
        print(line)
        self.assertEqual(line, S5_LINE)
        self.assertTrue(ok)


class S10Absent(Base):
    def test_S10_a_detail_without_bypass_actors_is_judged_and_equals_the_repository_ruleset_run(self):
        p, _, _ = run(self.prog, self.base, "ok")
        q, _, _ = run(self.prog, self.base, "repo")
        text = p.stdout + p.stderr
        errors = sum(1 for x in text.splitlines() if re.search(r"cannot read|error|Traceback", x, re.I))
        warns = text.count("bypass_actor") + text.count("WARN open: bypass")
        mentions = text.lower().count("bypass")
        same = 1 if (p.returncode, p.stdout, p.stderr) == (q.returncode, q.stdout, q.stderr) and q.returncode == 0 else 0
        line = ("S10: detail without bypass_actors: exit %d, errors %d, bypass_actor warnings %d, mentions of bypass %d, output equal to the same run on a "
                "repository ruleset with the field absent %d of 1" % (p.returncode, errors, warns, mentions, same))
        print(line)
        self.assertEqual(line, "S10: detail without bypass_actors: exit 0, errors 0, bypass_actor warnings 0, mentions of bypass 0, output equal to the same run on a "
                               "repository ruleset with the field absent 1 of 1")


class S4Marker(Base):
    """The marker is the token of every read and the answers carry planted text; the launcher's own pod spec, per-run Secrets, pod environment,
    audit file and run record (all from a real `create` and `stop` on the fake cluster) must hold neither."""

    def test_S4_the_marker_and_the_answer_text_are_in_none_of_five_places(self):
        sys.path.insert(0, HERE)
        sys.path.insert(0, LAUNCHER)
        import test_cloud_worker as tcw
        cw = tcw.cw
        pf, _, _ = run(self.prog, self.base, "ok")   # the preflight's own output is a sixth text; it is scanned too
        d = self.tmp.name
        state, rec = os.path.join(d, "kube.json"), os.path.join(d, "records")
        os.makedirs(rec)
        with open(state, "w") as f:
            json.dump({"pods": {}}, f)
        env = {"CW_KUBECTL": os.path.join(HERE, "fake_kubectl"), "FAKE_KUBE_STATE": state, "CW_CLOUD_AUDIT": os.path.join(d, "audit.jsonl"),
               "CW_CLOUD_IMAGE": "registry.example.internal/worker:1", "CW_TOKEN_ENDPOINT": "https://tokens.example.internal/mint",
               "CW_CLOUD_DOMAIN": "example.internal", "CW_KUBE_CONTEXT": "acme-cw", "CW_ALLOWED_REPOS": REPO, "CW_RUN_RECORD_DIR": rec,
               "CW_PREFLIGHT_TOKEN_CMD": "echo " + MARKER, "CW_GIT_TOKEN_CMD": "echo " + MARKER}
        clock = tcw.Clock()
        import io
        from contextlib import redirect_stdout, redirect_stderr

        def cli(*argv):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cw.main(list(argv), env, clock, lambda s: clock.advance(seconds=s))
            return code, out.getvalue(), err.getvalue()
        origin, work = os.path.join(d, "o.git"), os.path.join(d, "w")
        g = lambda *a, cwd=None: subprocess.run(["git"] + list(a), cwd=cwd, check=True, capture_output=True)
        g("init", "--bare", "-b", "main", origin)
        g("clone", origin, work)
        for k, v in (("user.name", "t"), ("user.email", "t@invalid")):
            g("config", k, v, cwd=work)
        with open(os.path.join(work, "a.txt"), "w") as f:
            f.write("one\n")
        g("add", "-A", cwd=work)
        g("commit", "-m", "init", cwd=work)
        g("push", "origin", "HEAD:refs/heads/seed", cwd=work)   # a seed branch: nothing here touches a default branch
        with open(state, "w") as f:
            json.dump({"pods": {}, "hook_env": {"CW_WORKDIR": work, "CW_WORKER": "coder7"}}, f)
        code, out, err = cli("create", "coder7", "--repo", REPO, "--branch", "main")
        self.assertEqual(code, 0, err)
        with open(state) as f:
            st = json.load(f)
        applied = st["applied"]
        pods = [o for o in applied if o["kind"] == "Pod"]
        self.assertEqual(len(pods), 1)
        places = {"pod spec": json.dumps(pods[0]),
                  "per-run Secrets": json.dumps([o for o in applied if o["kind"] == "Secret"] + [st.get("run_secrets", {})]),
                  "pod environment": json.dumps(pods[0]["spec"]["containers"][0]["env"])}
        code, out2, err2 = cli("stop", "coder7", "--reason", "operator")
        self.assertEqual(code, 0, err2)
        with open(env["CW_CLOUD_AUDIT"]) as f:
            places["audit file"] = f.read()
        recs = []
        for root, _, names in os.walk(rec):   # the run record directory
            for n in names:
                with open(os.path.join(root, n), errors="replace") as f:
                    recs.append(f.read())
        self.assertTrue(recs, "no run record was written offline")
        places["run record"] = "\n".join(recs)
        places["create output"] = out + err
        places["preflight output"] = pf.stdout + pf.stderr
        self.assertEqual(len(places), 7)
        five = ("pod spec", "per-run Secrets", "pod environment", "audit file", "run record")
        marker = sum(places[k].count(MARKER) for k in five)
        answer = sum(places[k].count(ANSWER) for k in places)
        line = "S4: marker occurrences in 5 places %d, answer text occurrences %d" % (marker, answer)
        print(line)
        self.assertEqual(line, "S4: marker occurrences in 5 places 0, answer text occurrences 0")
        self.assertEqual(sum(places[k].count(MARKER) for k in places), 0)


OLD_PATH = '("/repos/%s/rulesets/%d?includes_parents=true" % (target, rid))'
ORG_PATH = '("/orgs/%s/rulesets/%d" % (src, rid))'
OLD_403 = '                if st != "Organization" or e.status != 403 or e.limited:'
NEW_403 = '                if st != "Organization" or e.limited:'


class S7Mutants(Base):
    def mutant(self, old, new, name):
        text = open(os.path.join(LAUNCHER, "preflight.py")).read()
        self.assertEqual(text.count(old), 1)
        applied = text.count(old) == 1
        print("mutation applied", applied)
        return program_copy(self.tmp.name, name, text.replace(old, new))

    def test_S7_two_mutants_fail_their_test_and_the_documentation_says_it(self):
        failing = 0
        # mutant 1: the organization path again; S2 must fail on it
        m1 = self.mutant(OLD_PATH, ORG_PATH, "mutant1")
        failing += s2(m1, self.base) != S2_LINE
        # mutant 2: a 403 of the detail no longer fails closed (the ruleset counts as readable-and-judged: a pass with no rule counted is not
        # produced; here the 403 branch is made to skip, so no approval is needed and no FAIL is printed)
        m2 = self.mutant(OLD_403, '                if True:\n                    verdict[key] = True\n                    continue\n                if st != "Organization" or e.status != 403 or e.limited:', "mutant2")
        _, ok2 = s5(m2, self.base, self.tmp.name)
        failing += not ok2
        line = "S7: mutants failing their test %d of 2" % failing
        print(line)
        self.assertEqual(line, "S7: mutants failing their test 2 of 2")
        # and the real program passes both scenario tests (they discriminate)
        self.assertEqual(s2(os.path.join(self.tmp.name, "launcher", "preflight.py"), self.base), S2_LINE)

    def test_S7_docs_the_readme_says_repository_level_and_not_seen_and_not_the_old_sentence(self):
        with open(os.path.join(LAUNCHER, "README.md")) as f:
            text = " ".join(f.read().split())
        found = sum(1 for s in ("read at repository level", "bypass actors of organization rulesets are not returned") if s in text)
        old = text.count("can never read an organization ruleset")
        line = "S7 docs: %d of 2 sentences found, old sentence %d of 1" % (found, old)
        print(line)
        self.assertEqual(line, "S7 docs: 2 of 2 sentences found, old sentence 0 of 1")


if __name__ == "__main__":
    unittest.main()
