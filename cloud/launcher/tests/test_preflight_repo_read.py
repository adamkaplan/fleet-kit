"""#1573: the preflight reads an organization ruleset at repository level (S2, S4, S5, S7, S10). Offline: its own fake GitHub on 127.0.0.1
that records every request path; the real preflight.py runs as a subprocess against it (nothing is imported from test_preflight.py)."""
import hashlib
import http.server
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
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


ID_LINE = "preflight: organization ruleset 9: read at repository level, counted"


def s2(prog, base):
    p, paths, orgs = run(prog, base, "ok")
    named = sum(1 for x in p.stdout.splitlines() if x.strip() == ID_LINE)   # the ruleset is named by its identifier on one line
    reqs = sum(1 for x in paths if x == "/repos/%s/rulesets/9" % REPO)
    nv = sum(1 for x in p.stdout.splitlines() if "NOT VERIFIED" in x)
    passing = 1 if p.returncode == 0 and "preflight ok" in p.stdout else 0
    return ("S2: repositories passing %d of 1, ruleset 9 named %d line, NOT VERIFIED lines %d, requests to the repository-level path %d, requests under /orgs/ %d"
            % (passing, named, nv, reqs, orgs))


S2_LINE = ("S2: repositories passing 1 of 1, ruleset 9 named 1 line, NOT VERIFIED lines 0, requests to the repository-level path 1, requests under /orgs/ 0")


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
        p, _, _ = run(self.prog, self.base, "ok")
        self.assertEqual([x for x in p.stdout.splitlines() if x.strip() == ID_LINE], [ID_LINE])


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
        a, b = (p.stdout + p.stderr).splitlines(), (q.stdout + q.stderr).splitlines()
        self.assertEqual(a.count(ID_LINE), 1)
        a.remove(ID_LINE)
        differing = sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
        differing += 1   # the removed identifier line is the one line that differs
        self.assertEqual(q.returncode, 0)
        line = ("S10: detail without bypass_actors: exit %d, errors %d, bypass_actor warnings %d, mentions of bypass %d, differing lines from the "
                "repository-ruleset run %d (the identifier line)" % (p.returncode, errors, warns, mentions, differing))
        print(line)
        self.assertEqual(line, "S10: detail without bypass_actors: exit 0, errors 0, bypass_actor warnings 0, mentions of bypass 0, differing lines from the "
                               "repository-ruleset run 1 (the identifier line)")


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

    def test_S14_a_mutant_without_the_identifier_line_fails_the_S2_test(self):
        text = open(os.path.join(LAUNCHER, "preflight.py")).read()
        old = '            if verdict[key] and st == "Organization": say("organization ruleset %d: read at repository level, counted" % rid)\n'
        self.assertEqual(text.count(old), 1)
        applied = text.count(old) == 1
        m = program_copy(self.tmp.name, "mutant14", text.replace(old, ""))
        failing = 1 if s2(m, self.base) != S2_LINE else 0
        real_ids = sum(1 for x in run(self.prog, self.base, "repo")[0].stdout.splitlines() if x.strip() == ID_LINE)
        copy_ids = sum(1 for x in run(m, self.base, "repo")[0].stdout.splitlines() if x.strip() == ID_LINE)
        line = ("S14: mutants failing the S2 test %d of 1, identifier lines for a repository ruleset %d, mutation applied %s"
                % (failing, real_ids + copy_ids, applied))
        print(line)
        self.assertEqual(line, "S14: mutants failing the S2 test 1 of 1, identifier lines for a repository ruleset 0, mutation applied True")

    def test_S7_docs_the_readme_says_repository_level_and_not_seen_and_not_the_old_sentence(self):
        with open(os.path.join(LAUNCHER, "README.md")) as f:
            text = " ".join(f.read().split())
        found = sum(1 for s in ("read at repository level", "bypass actors of organization rulesets are not returned") if s in text)
        old = text.count("can never read an organization ruleset")
        line = "S7 docs: %d of 2 sentences found, old sentence %d of 1" % (found, old)
        print(line)
        self.assertEqual(line, "S7 docs: 2 of 2 sentences found, old sentence 0 of 1")



WRAPPER = '''import os, runpy, sys
os.environ["CW_GITHUB_API_BASE"] = %r
os.environ["CW_PREFLIGHT_TOKEN_CMD"] = %r
sys.path.insert(0, %r)
sys.argv = [%r] + sys.argv[1:]
runpy.run_path(%r, run_name="__main__")
'''
LAUNCH_DRIVER = '''import os, sys
sys.path.insert(0, %r)
sys.path.append(%r)
import run_task
run_task.cw.PREFLIGHT_ARGV = [sys.executable, %r]
sys.exit(run_task.main())
'''


class S4Real(Base):
    """S4 made real: ONE launch (the operator driver, run_task.main) runs the REAL preflight.py through the launcher's PREFLIGHT_ARGV hook (the child's own
    environment is scrubbed, so a small wrapper sets the fake's API base and the marker token command, then runs the real program); the launch then really
    creates and stops a worker on the fake cluster; the five places of THAT launch and the preflight's output are scanned."""

    def launch(self, program_dir, planted_in_audit=False):
        sys.path.insert(0, HERE)
        import test_driver as td
        drv = td.Driver("test_s5_a_normal_end_runs_the_task_stops_the_worker_and_cleans_up")
        drv.setUp()
        self.addCleanup(drv.tmp.cleanup)
        d = drv.tmp.name
        prog = os.path.join(program_dir, "preflight.py")
        wrapper, driver = os.path.join(d, "wrap_preflight.py"), os.path.join(d, "drive.py")
        with open(wrapper, "w") as f:
            f.write(WRAPPER % (self.base, "echo " + MARKER, program_dir, prog, prog))
        with open(driver, "w") as f:
            f.write(LAUNCH_DRIVER % (program_dir, os.path.join(REPO_ROOT, "bin"), wrapper))
        rec = os.path.join(d, "records")
        os.makedirs(rec)
        Fake.detail, Fake.paths, Fake.orgs = "ok", [], 0
        args = [sys.executable, driver] + drv.args()[2:]
        p = subprocess.run(args, env=dict(drv.denv, CW_RUN_RECORD_DIR=rec), capture_output=True, text=True, timeout=90)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        st = drv.st()
        applied = st["applied"]
        pods = [o for o in applied if o["kind"] == "Pod"]
        self.assertEqual(len(pods), 1)
        places = {"pod spec": json.dumps(pods[0]),
                  "per-run Secrets": json.dumps([o for o in applied if o["kind"] == "Secret"] + [st.get("run_secrets", {})]),
                  "pod environment": json.dumps(pods[0]["spec"]["containers"][0]["env"]),
                  "audit file": drv.read(drv.env["CW_CLOUD_AUDIT"])}
        recs = []
        for root, _, names in os.walk(rec):
            for n in names:
                with open(os.path.join(root, n), errors="replace") as f:
                    recs.append(f.read())
        self.assertTrue(recs, "no run record was written offline")
        places["run record"] = "\n".join(recs)
        places["preflight output"] = out   # the driver prints the preflight's result line and its refusal text; the launch's whole output
        ran = sum(1 for x in Fake.paths if x == "/repos/%s/rulesets/9" % REPO)
        self.assertIn("preflight: preflight ok", out)
        return places, ran

    FIVE = ("pod spec", "per-run Secrets", "pod environment", "audit file", "run record")

    def test_S4_real_the_preflight_runs_in_one_launch_and_nothing_is_in_five_places(self):
        places, ran = self.launch(LAUNCHER)
        marker = sum(places[k].count(MARKER) for k in self.FIVE)
        answer = sum(places[k].count(ANSWER) for k in places)
        pfout = places["preflight output"].count(MARKER) + places["preflight output"].count(ANSWER)
        line = ("S4 real: preflight ran in the launch %d of 1 (requests to the repository-level path %d), marker occurrences in 5 places %d, "
                "answer text occurrences %d, preflight output occurrences %d" % (1 if ran else 0, ran, marker, answer, pfout))
        print(line)
        self.assertEqual(line, "S4 real: preflight ran in the launch 1 of 1 (requests to the repository-level path 1), marker occurrences in 5 places 0, "
                               "answer text occurrences 0, preflight output occurrences 0")

    def test_S4_real_mutant_a_launcher_that_writes_the_preflight_result_into_the_audit_is_found(self):
        d = os.path.join(self.tmp.name, "mut", "cloud", "launcher")
        os.makedirs(d)
        os.makedirs(os.path.join(self.tmp.name, "mut", "bin"))
        shutil.copy(os.path.join(REPO_ROOT, "bin", "path_walk.py"), os.path.join(self.tmp.name, "mut", "bin"))   # the launcher finds it by its own place
        for n in os.listdir(LAUNCHER):
            if n.endswith(".py") or n == "repo-approvals.json":
                shutil.copy(os.path.join(LAUNCHER, n), d)
        # the result line of the preflight carries the token command (the planted text) and the launcher writes that line into the audit file
        old_pf = '        say("preflight ok: " + ", ".join(sorted(t for t in types if isinstance(t, str))) + warn_tail)\n'
        new_pf = '        say("preflight ok: " + ", ".join(sorted(t for t in types if isinstance(t, str))) + warn_tail + " " + os.environ.get("CW_PREFLIGHT_TOKEN_CMD", ""))\n'
        old_cw = "    return scrub(lines[-1], (), 300)\n\n\n# ----------------------------------------------------------------"
        new_cw = ("    write_audit(audit_path(env), {\"action\": \"preflight\", \"reason\": lines[-1]})\n"
                  "    return scrub(lines[-1], (), 300)\n\n\n# ----------------------------------------------------------------")
        applied = 0
        for name, old, new in (("preflight.py", old_pf, new_pf), ("cloud_worker.py", old_cw, new_cw)):
            path = os.path.join(d, name)
            with open(path) as f:
                text = f.read()
            self.assertEqual(text.count(old), 1, name)
            applied += 1
            with open(path, "w") as f:
                f.write(text.replace(old, new))
        places, ran = self.launch(d)
        in_audit = places["audit file"].count(MARKER) + places["audit file"].count(ANSWER)
        found = sum(places[k].count(MARKER) for k in self.FIVE)
        fails = 1 if found != 0 else 0   # the S4 real assertion (zero in 5 places) fails on the mutant
        line = "S4 real mutant: marker/answer occurrences in audit %d, test fails %d of 1, mutation applied %s" % (in_audit, fails, applied == 2)
        print(line)
        self.assertEqual(line, "S4 real mutant: marker/answer occurrences in audit 1, test fails 1 of 1, mutation applied True")



MINT_ROLES_TESTS = 32   # the plan's count: '32 of 32 tests of test_mint_roles pass'


class S3Mints(Base):
    """S3: the preflight mints no token of another role. Mechanism: the file runs the preflight as a subprocess, so a mock patch is not possible;
    the scratch program copy gets an app_token.py whose mint() appends the role of every call to a file and returns the planted marker."""
    OLD_MINT = "            return app_token.mint(target)\n"

    def recording_copy(self, name, preflight_text=None):
        prog = program_copy(self.tmp.name, name, preflight_text)
        at = os.path.join(os.path.dirname(prog), "app_token.py")
        with open(at, "a") as f:
            f.write("\n\ndef mint(target, role=\"worker\"):\n    with open(os.environ[\"CW_MINT_RECORD\"], \"a\") as r:\n"
                    "        r.write(role + \"\\n\")\n    return \"" + MARKER + "\"\n")
        return prog

    def mints(self, prog):
        rec = os.path.join(self.tmp.name, "record-" + os.path.basename(os.path.dirname(prog)))
        Fake.detail, Fake.paths, Fake.orgs = "ok", [], 0
        env = {k: v for k, v in os.environ.items() if k != "CW_PREFLIGHT_TOKEN_CMD"}
        env.update(CW_GITHUB_API_BASE=self.base, CW_MINT_RECORD=rec)
        p = subprocess.run([sys.executable, prog, REPO], env=env, capture_output=True, text=True, timeout=60)
        roles = []
        if os.path.exists(rec):
            with open(rec) as f:
                roles = f.read().split()
        return p, roles

    def check(self, prog):
        """The S3 check: raises AssertionError naming every other role; returns the recorded roles."""
        p, roles = self.mints(prog)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(roles, "no mint was recorded")
        other = [r for r in roles if r not in ("worker", "orchestrator")]
        if other:
            raise AssertionError("mint for another role: " + ", ".join(sorted(set(other))))
        self.assertTrue(set(roles) <= {"worker", "orchestrator"})
        return roles

    def test_S3_the_preflight_mints_only_worker_and_orchestrator_and_the_mutant_is_found(self):
        prog = self.recording_copy("recorded")
        roles = self.check(prog)
        other = sum(1 for r in roles if r not in ("worker", "orchestrator"))
        self.assertEqual(other, 0)
        t0 = time.monotonic()
        child = subprocess.Popen([sys.executable, "-B", "-m", "unittest", "test_mint_roles"], cwd=HERE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            _out, err = child.communicate(timeout=300)   # a hang guard only: the verdict is the exit code and the counts
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
            child.communicate()
            raise AssertionError("the test_mint_roles run did not end within 300 seconds (hang guard)")
        finally:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
        took = time.monotonic() - t0
        m = re.search(r"Ran (\d+) tests?", err)
        self.assertIsNotNone(m, err[-300:])
        self.assertEqual(child.returncode, 0, err[-300:])
        self.assertTrue(re.search(r"^OK\s*$", err, re.M), err[-300:])
        n = int(m.group(1))
        self.assertEqual(n, MINT_ROLES_TESTS)
        line = "S3: mints for other roles %d, test_mint_roles %d of %d (whole file, %d expected), took %.1f s" % (other, n, n, MINT_ROLES_TESTS, took)
        print(line)
        # the mutant: the preflight mints once more for another role
        text = open(os.path.join(LAUNCHER, "preflight.py")).read()
        self.assertEqual(text.count(self.OLD_MINT), 1)
        applied = text.count(self.OLD_MINT) == 1
        mut = self.recording_copy("mutant3", text.replace(self.OLD_MINT, '            app_token.mint(target, "reviewer")\n' + self.OLD_MINT))
        control = 1
        try:
            self.check(self.recording_copy("control3"))
        except AssertionError:
            control = 0
        failed = named = 0
        try:
            self.check(mut)
        except AssertionError as e:
            failed = 1
            named = 1 if re.search(r"another role: .*\breviewer\b", str(e)) else 0
        line = ("S3 mutant: control passes %d of 1, mutant fails %d of 1, failure names the role %d of 1, mutation applied %s"
                % (control, failed, named, applied))
        print(line)
        self.assertEqual(line, "S3 mutant: control passes 1 of 1, mutant fails 1 of 1, failure names the role 1 of 1, mutation applied True")


if __name__ == "__main__":
    unittest.main()
