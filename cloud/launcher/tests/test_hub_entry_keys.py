"""Ask #1475: the launcher writes repo (and issue, when the create names one) in the hub entry of a registered worker.
Offline: the fake kubectl beside this file is the cluster; the switchboard side runs with fakes for the cluster and the launcher."""
import io
import json
import os
import re
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
import cloud_worker as cw  # noqa: E402
from test_cloud_worker import Base  # noqa: E402
from test_stack_model_key import load_bin  # noqa: E402

REPO = "acme-org/widget-service"
REGISTRY = "cloud-hub-registry"
BASE_KEYS = {"url", "session", "password_file"}


def source(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def mutant_module(old, new):
    """The launcher with ONE source edit, applied (asserted), run as a module of its own."""
    text = source("cloud/launcher/cloud_worker.py")
    assert text.count(old) == 1, "the mutation does not apply: %r" % old
    mod = types.ModuleType("cw_mutant_1475")
    mod.__file__ = os.path.join(LAUNCHER, "cloud_worker.py")
    exec(compile(text.replace(old, new), mod.__file__, "exec"), mod.__dict__)
    return mod


class EntryBase(Base):
    module = cw

    def setUp(self):
        super().setUp()
        self.patch_state(secrets={REGISTRY: 1, "cloud-hub-pw": 1}, secret_bodies={"cloud-hub-pw": {}, REGISTRY: {"secrets.json": "{}", "agents.json": "{}"}},
                         hook_result={"pushed": False, "wip_branch": None})

    def make(self, worker, *extra):
        out, err = io.StringIO(), io.StringIO()
        argv = ["create", worker, "--repo", REPO, "--branch", "main", *extra]
        with redirect_stdout(out), redirect_stderr(err):
            code = self.module.main(argv, self.env, self.clock, lambda s: self.clock.advance(seconds=s))
        return code, out.getvalue(), err.getvalue()

    def entry(self, worker):
        return json.loads(self.st()["secret_bodies"][REGISTRY]["agents.json"]).get(worker)

    def orchestrator(self, *extra):
        code, _, err = self.make("orch1", "--agent", "orchestrator", "--boss", "boss1", *extra)
        self.assertEqual(code, 0, err)
        return self.entry("orch1")

    def coder_hub_on(self, *extra):
        self.env["CW_HUB_CHANNEL_URL"] = "http://hub.example.internal:9000"
        code, _, err = self.make("coder7", *extra)
        self.assertEqual(code, 0, err)
        return self.entry("coder7")

    def check_with_issue(self, entry):
        self.assertEqual(set(entry), BASE_KEYS | {"repo", "issue"})
        self.assertEqual(entry["repo"], REPO)
        self.assertEqual(entry["issue"], 7)
        self.assertEqual(type(entry["issue"]), int)
        self.assertEqual(entry["session"], "")
        self.assertTrue(entry["url"].startswith("http://") and entry["url"].endswith(":4096"))
        self.assertRegex(entry["password_file"], r"^/etc/hub/pw/(orch1|coder7)$")

    def check_without_issue(self, entry):
        self.assertEqual(set(entry), BASE_KEYS | {"repo"})
        self.assertEqual(entry["repo"], REPO)
        self.assertNotIn("issue", entry)


class Entries(EntryBase):
    def test_s2_an_orchestrator_entry_holds_repo_and_a_numeric_issue(self):
        self.check_with_issue(self.orchestrator("--issue", "7"))

    def test_s2_a_registered_coder_entry_holds_repo_and_a_numeric_issue(self):
        self.check_with_issue(self.coder_hub_on("--issue", "7"))

    def test_s3_without_an_issue_the_entry_has_repo_and_no_issue_key(self):
        self.check_without_issue(self.orchestrator())
        self.check_without_issue(self.coder_hub_on())

    def test_s5_a_coder_with_the_hub_off_registers_no_entry(self):
        code, _, err = self.make("coder7", "--issue", "7")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.entry("coder7"))
        self.assertEqual(json.loads(self.st()["secret_bodies"][REGISTRY]["agents.json"]), {})

    def test_a_bad_issue_is_a_usage_error_and_nothing_is_created(self):
        for bad in ("abc", "0", "-3", "1.5", "7x", ""):
            with self.subTest(bad=bad):
                code, _, err = self.make("coder7", "--issue=" + bad)
                self.assertEqual(code, cw.EXIT_USAGE, err)
                self.assertIn("--issue", err)
                self.assertFalse(os.path.exists(self.env["FAKE_KUBE_CALLS"]), "a cluster call was made")
                self.assertEqual(self.st()["pods"], {})


class Mutants(EntryBase):
    def run_mutant(self, old, new, check, *args):
        self.module = mutant_module(old, new)
        with self.assertRaises(AssertionError):
            check(*args)

    def test_omitting_repo_fails_s2(self):
        self.run_mutant('entry["repo"] = repo', "pass", lambda: self.check_with_issue(self.orchestrator("--issue", "7")))

    def test_issue_as_text_fails_s2(self):
        self.run_mutant('entry["issue"] = issue', 'entry["issue"] = str(issue)', lambda: self.check_with_issue(self.orchestrator("--issue", "7")))

    def test_always_writing_issue_fails_s3(self):
        self.run_mutant('if issue is not None:\n            entry["issue"] = issue', 'entry["issue"] = issue or 0', lambda: self.check_without_issue(self.orchestrator()))

    def test_registering_every_create_fails_s5(self):
        def hub_off():
            code, _, err = self.make("coder7", "--issue", "7")
            self.assertEqual(code, 0, err)
            self.assertIsNone(self.entry("coder7"))
        self.run_mutant('hub = agent == "orchestrator" or hub_on(self.env)', "hub = True", hub_off)


class Sentinel(Exception):
    pass


class FakeCluster:
    entered = 0

    def __init__(self, recovery):
        pass

    def __enter__(self):
        FakeCluster.entered += 1
        return self

    def __exit__(self, *a):
        return False

    def add(self, *a):
        pass

    def clean(self, text, n=100):
        return text


class SwitchboardSide(unittest.TestCase):
    """launch --cloud with --issue: the create gets '--issue N'; a bad number is refused before anything is created."""

    def setUp(self):
        import tempfile
        self.state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_dir.cleanup)
        self.bin = load_bin()

    def launch(self, agent, issue):
        b = self.bin
        seen = {"created": []}

        class Eng:
            def discover(self, now):
                return types.SimpleNamespace(agents={})

            def now(self):
                return 0

        def run_launcher(runner, command, worker, options=(), *a, **kw):
            seen["created"].append([command, worker, list(options)])
            raise Sentinel()
        lib = types.SimpleNamespace(check_repo_preflight=lambda repo, env: "ok")
        names = ("launcher_argv", "use_settings", "check_allowed", "_lib", "load_record", "H", "run_launcher", "Cluster", "model_entry", "git_token", "stack_model_secret", "_audit")
        saved = {k: getattr(b, k) for k in names}
        h = dict(b.H, LaunchError=type("LaunchError", (Exception,), {}), NAME_RE=re.compile("[a-z0-9]+"), RESERVED_AGENT_NAMES=(), LANE_NAME_PREFIX="lane-", PROG="sb",
                 launch_repo=lambda a: a.repo, build_engine=lambda r, n, c: (Eng(), self.state_dir.name, None), read_brief=lambda f, i: "brief")
        args = types.SimpleNamespace(name="coder7", resume=False, headless=False, agent=agent, reports_to="boss1", dir=None, repo=REPO, branch="work-branch",
                                     shape=None, idle=None, hard=None, brief_file=None, stack=False, issue=issue)
        FakeCluster.entered = 0
        try:
            b.launcher_argv, b.use_settings, b.check_allowed = (lambda: ["x"]), (lambda: "test"), (lambda repo: None)
            b._lib, b.load_record, b.H, b.run_launcher, b.Cluster = (lambda: lib), (lambda st, n: None), h, run_launcher, FakeCluster
            b._audit = lambda *a, **k: None
            b.model_entry, b.git_token, b.stack_model_secret = (lambda: "m"), (lambda repo, cluster: "t"), (lambda: None)
            err = None
            try:
                with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    b._launch(args, None, lambda: 0, lambda st: None)
            except Sentinel:
                pass
            except Exception as e:   # the refusal
                err = e
        finally:
            for k, v in saved.items():
                setattr(b, k, v)
        return seen, err

    def test_s4_a_whole_number_is_passed_to_the_create(self):
        for agent in ("orchestrator", "coder"):
            with self.subTest(agent=agent):
                seen, err = self.launch(agent, 7)
                self.assertIsNone(err)
                (command, worker, options), = [c for c in seen["created"] if c[0] == "create"]
                self.assertEqual(command, "create")
                self.assertEqual(options[options.index("--issue") + 1], "7")
                self.assertEqual(options.count("--issue"), 1)

    def test_s4_no_issue_passes_no_option(self):
        seen, err = self.launch("coder", None)
        self.assertNotIn("--issue", [c for c in seen["created"] if c[0] == "create"][0][2])

    def test_s4_a_bad_number_is_refused_before_anything_is_created(self):
        for agent in ("orchestrator", "coder"):
            for bad in ("abc", 0, -3, "0", "-3", "1.5"):
                with self.subTest(agent=agent, bad=bad):
                    seen, err = self.launch(agent, bad)
                    self.assertIsNotNone(err)
                    self.assertIn("--issue", str(err))
                    self.assertIn("nothing was created", str(err))
                    self.assertEqual(seen["created"], [])   # the launcher was not called at all
                    self.assertEqual(FakeCluster.entered, 0)


class Docs(unittest.TestCase):
    def test_s6_the_docs_name_repo_issue_and_who_writes_them(self):
        text = source("docs/cloud-channel.md")
        section = text[text.index("### agents.json"):text.index("### secrets.json")]
        for phrase in ('"repo": "acme-org/widget-service"', "- `repo`: the repository", "written by the launcher at registration",
                       "a whole number above 0", "never text", "from the create's `--issue`", "The launcher, not the person or the agent, writes `repo` and `issue`"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, " ".join(section.split()))


if __name__ == "__main__":
    unittest.main()
