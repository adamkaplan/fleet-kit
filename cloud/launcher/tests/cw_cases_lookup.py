"""Test cases executed by bin/test-cloud-worker in its own namespace; do not run alone."""
FAKE_CHECK = """import json, os, sys, time
from pathlib import Path
root = Path(__file__).resolve().parents[2]
cfg = json.loads((root / "fake.json").read_text())
def note(kind):
    with open(str(root / "runs.jsonl"), "a") as f:
        f.write(json.dumps({"kind": kind, "t": time.time(), "argv": sys.argv[1:], "cwd": os.getcwd(), "env": sorted(os.environ), "pid": os.getpid(),
                            "pgid": os.getpgid(0), "home": os.environ.get("HOME")}) + "\\n")
note("start")
if cfg.get("grandchild"):
    import subprocess
    sub = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])   # keeps the check's stdout open
    with open(str(root / "grandchild.pid"), "w") as f:
        f.write(str(sub.pid))
time.sleep(cfg.get("sleep", 0))
if cfg.get("fixes"):
    (root / "fixed").write_text("x")
print("checking address " + cfg.get("address", "addr-marker"))
print(cfg.get("line", "rules 3 -> 4, 2026-10-19"))
sys.stderr.write("address " + cfg.get("address", "addr-marker") + "\\n")
note("end")
sys.exit(cfg["code"])
"""

AZ_VAULT = """#!/bin/sh
echo run >> "$STUB_DIR/az.log"
mode=$(cat "$STUB_DIR/az_mode" 2>/dev/null)
if [ "$mode" = firewall ] && [ ! -e "$FAKE_CHECKOUT/fixed" ]; then
  echo "(Forbidden) Client address is not authorized and caller is not a trusted service. Code: Forbidden Inner error: ForbiddenByFirewall" >&2
  exit 1
fi
if [ "$mode" = down ]; then
  echo "vault unreachable" >&2
  exit 1
fi
if [ "$mode" = auth ]; then
  echo "(Forbidden) The user, group or application does not have secrets get permission on key vault. Inner error: AccessDenied" >&2
  exit 1
fi
cat "$STUB_DIR/admin_kubeconfig"
"""


class TestLauncherLookup(Base):
    """S17: the launcher is the variable's program, else the checkout's own, else `cloud-worker` on the PATH, else a refusal (exit 2, zero calls)."""

    def setUp(self):
        super().setUp()
        self.mod = sb.load_module("cloud_worker")
        self.fake = Path(os.path.realpath(self.home)) / "checkout"
        self.entry = self.fake / "cloud" / "launcher" / "cloud-worker"
        self.entry.parent.mkdir(parents=True)
        shutil.copy(str(self.stub), str(self.entry))
        self.entry.chmod(0o755)
        self.bare = self.home / "bare-path"
        self.bare.mkdir()
        moved = self.stub_dir / "launcher-stub"            # nothing called cloud-worker is on the PATH (the stub directory stays on it for kubectl and az)
        self.stub.rename(moved)
        self.stub = moved
        os.environ["PATH"] = "%s:%s" % (self.bare, os.environ["PATH"])
        patch = mock.patch.object(self.mod.cloud_cluster, "checkout_launcher_path", lambda: self.entry)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("FLEET_CLOUD_WORKER_BIN", None)

    def test_an_empty_environment_uses_the_launcher_of_the_checkout(self):
        code, out, err = self.launch()
        self.assertEqual(code, 0, err)
        self.assertEqual(sum(1 for c in self.stub_calls() if "create" in c), 1)   # the checkout's launcher was called once

    def test_the_variable_still_wins_over_the_checkout(self):
        other = self.home / "other"
        other.mkdir()
        elsewhere = other / "cloud-worker"
        elsewhere.write_text("#!/bin/sh\nexit 7\n")
        elsewhere.chmod(0o755)
        os.environ["FLEET_CLOUD_WORKER_BIN"] = str(elsewhere)
        code, out, err = self.launch()
        self.assertEqual(code, 1)
        self.assertEqual(self.stub_calls(), [])           # the checkout's launcher was never called

    def test_the_path_is_the_last_resort_before_the_refusal(self):
        self.entry.chmod(0o644)                            # the checkout's is unusable
        path_launcher = self.bare / "cloud-worker"
        shutil.copy(str(self.stub), str(path_launcher))
        path_launcher.chmod(0o755)
        self.assertEqual(self.mod.launcher_argv(), ["cloud-worker"])

    def refused(self):
        code, out, err = self.launch()
        self.assertEqual(code, 2, err)
        self.assertIn("FLEET_CLOUD_WORKER_BIN", err)
        self.assertIn(str(self.entry), err)
        self.assertEqual(self.stub_calls(), [])
        self.assertEqual(sum("no cloud launcher found" in line for line in err.splitlines()), 1)
        self.assertFalse((self.state / "cloud" / "coder7.json").exists())

    def test_with_neither_it_refuses_naming_both_places_and_calls_nothing(self):
        self.entry.unlink()
        self.refused()

    def test_an_unsafe_checkout_launcher_is_not_used(self):
        """One case per judgement of the one walk: not executable, group-writable, world-writable, a link naming a path, a directory, a writable parent."""
        def not_exec():
            self.entry.chmod(0o644)

        def group_writable():
            self.entry.chmod(0o775)

        def world_writable():
            self.entry.chmod(0o757)

        def link_elsewhere():
            self.entry.unlink()
            os.symlink(str(self.stub), str(self.entry))

        def directory():
            self.entry.unlink()
            self.entry.mkdir()

        def parent_writable():
            self.entry.parent.chmod(0o777)
            self.addCleanup(self.entry.parent.chmod, 0o755)

        for case in (not_exec, group_writable, world_writable, link_elsewhere, directory, parent_writable):
            with self.subTest(case=case.__name__):
                self.setUp_again()
                case()
                self.assertIsNotNone(self.mod._checkout_launcher_problem(str(self.entry)))
                self.assertRaises(self.mod.Refusal, self.mod.launcher_argv)

    def setUp_again(self):
        if self.entry.is_dir() and not self.entry.is_symlink():
            self.entry.rmdir()
        elif self.entry.is_symlink() or self.entry.exists():
            self.entry.unlink()
        self.entry.parent.chmod(0o755)
        shutil.copy(str(self.stub), str(self.entry))
        self.entry.chmod(0o755)

    def test_the_kits_own_launcher_link_is_accepted(self):
        """The real checkout's `cloud-worker` is a link to its sibling cloud_worker.py: judged by that file, the entry used is the link."""
        real = Path(self.mod.__file__).resolve().parent.parent / "cloud" / "launcher" / "cloud-worker"
        self.assertTrue(real.is_symlink())
        self.assertIsNone(self.mod._checkout_launcher_problem(str(real)))


class TestLauncherLookupInputs(Base):
    """S17, the whole family: every INPUT the lookup could read (the variable, the checkout's own file, the working directory, the PATH, a symlinked
    checkout, an unsafe checkout) x the launcher it must give. The REAL checkout_launcher_path runs: only the module's own location is moved."""

    def setUp(self):
        super().setUp()
        self.mod = sb.load_module("cloud_worker")
        self.base = Path(os.path.realpath(self.home))
        self.checkout = self.make_checkout(self.base / "checkout")
        self.decoy = self.make_checkout(self.base / "decoy")           # a valid-looking checkout the working directory holds
        self.on_path = self.base / "on-path"
        self.on_path.mkdir()
        shutil.copy(str(self.stub), str(self.on_path / "cloud-worker"))
        (self.on_path / "cloud-worker").chmod(0o755)
        os.environ.pop("FLEET_CLOUD_WORKER_BIN", None)
        self.cwd = os.getcwd()
        self.addCleanup(os.chdir, self.cwd)

    def make_checkout(self, root):
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "cloud_worker.py").write_text("")
        (root / "cloud" / "launcher").mkdir(parents=True)
        entry = root / "cloud" / "launcher" / "cloud-worker"
        shutil.copy(str(self.stub), str(entry))
        entry.chmod(0o755)
        return root

    def lookup(self, module_dir=None, path=None, cwd=None, variable=None):
        """launcher_argv() with the module located in module_dir/bin, the PATH given (the stub directory kept for kubectl and az), the cwd given."""
        module_dir = module_dir or self.checkout
        env = {"PATH": path if path is not None else "/usr/bin:/bin"}
        if variable:
            env["FLEET_CLOUD_WORKER_BIN"] = variable
        os.chdir(str(cwd or self.cwd))
        with mock.patch.object(self.mod, "__file__", str(module_dir / "bin" / "cloud_worker.py")), mock.patch.dict(os.environ, env):
            return self.mod.launcher_argv()

    def entry_of(self, root):
        return str(Path(os.path.realpath(root)) / "cloud" / "launcher" / "cloud-worker")

    def test_the_table_of_inputs_and_the_launcher_each_gives(self):
        path = str(self.on_path) + ":/usr/bin:/bin"
        link = self.base / "link-to-checkout"
        os.symlink(str(self.checkout), str(link))
        table = [
            ("the checkout's own file, nothing else", dict(), self.entry_of(self.checkout)),
            ("the working directory holds another checkout", dict(cwd=self.decoy), self.entry_of(self.checkout)),
            ("a different cloud-worker on the PATH", dict(path=path), self.entry_of(self.checkout)),                      # the checkout comes BEFORE the PATH
            ("the variable wins over both", dict(path=path, cwd=self.decoy, variable="/opt/acme/launcher"), "/opt/acme/launcher"),
            ("a symlinked checkout: judged on its real parents, the real path is run", dict(module_dir=link), self.entry_of(self.checkout)),
        ]
        for label, args, expected in table:
            with self.subTest(label):
                self.assertEqual(self.lookup(**args), [expected])

    def test_an_unsafe_checkout_launcher_never_falls_to_the_working_directory(self):
        (self.checkout / "cloud" / "launcher" / "cloud-worker").chmod(0o644)
        with self.assertRaises(self.mod.Refusal) as caught:
            self.lookup(cwd=self.decoy)
        self.assertIn(self.entry_of(self.checkout), str(caught.exception))
        self.assertNotIn(str(self.decoy), str(caught.exception))

    def test_an_unsafe_checkout_launcher_falls_to_the_path_and_the_working_directory_is_never_looked_in(self):
        (self.checkout / "cloud" / "launcher" / "cloud-worker").chmod(0o644)
        self.assertEqual(self.lookup(cwd=self.decoy, path=str(self.on_path) + ":/usr/bin:/bin"), ["cloud-worker"])


    # ---- the ordering class: one rule per adjacent pair of LAUNCHER_ORDER, one mutant that picks the OTHER candidate

    def candidates(self, name):
        """What it takes for source `name` to offer a launcher, and the argv it then gives: (lookup arguments, expected argv)."""
        return {"variable": (dict(variable="/opt/acme/launcher"), ["/opt/acme/launcher"]),
                "checkout": (dict(), [self.entry_of(self.checkout)]),
                "path": (dict(path=str(self.on_path) + ":/usr/bin:/bin"), ["cloud-worker"])}[name]

    def ordering_failures(self):
        """Every rule the lookup must keep, checked on the module as it is now: the failures it names (an empty list on the real build)."""
        bad = []
        order = self.mod.LAUNCHER_ORDER
        for i, early in enumerate(order[:-1]):
            for late in order[i + 1:]:
                args = {**self.candidates(late)[0], **self.candidates(early)[0]}
                if early == "checkout":
                    args.pop("variable", None)
                want = self.candidates(early)[1]                        # the earlier source's launcher must be the one given
                try:
                    got = self.lookup(**args)
                except self.mod.Refusal:
                    got = "refused"
                if got != want:
                    bad.append("%s before %s" % (early, late))
        # the checkout is vetted: an unsafe one is skipped, and the working directory is never a source
        (self.checkout / "cloud" / "launcher" / "cloud-worker").chmod(0o644)
        try:
            got = self.lookup(cwd=self.decoy, path=str(self.on_path) + ":/usr/bin:/bin")
        except self.mod.Refusal:
            got = "refused"
        if got != ["cloud-worker"]:
            bad.append("an unsafe checkout falls to the PATH, never to the working directory")
        try:
            self.lookup(cwd=self.decoy)
            bad.append("the working directory is a source")
        except self.mod.Refusal:
            pass
        (self.checkout / "cloud" / "launcher" / "cloud-worker").chmod(0o755)
        try:
            got = self.lookup(cwd=self.decoy)
        except self.mod.Refusal:
            got = "refused"
        if got != [self.entry_of(self.checkout)]:
            bad.append("the working directory beats the checkout")
        return bad

    def test_the_real_lookup_keeps_every_ordering_rule(self):
        self.assertEqual(self.mod.LAUNCHER_ORDER, ("variable", "checkout", "path"))
        self.assertEqual(self.ordering_failures(), [])

    def test_a_mutant_per_rule_is_caught(self):
        real_argv, real_path = self.mod.launcher_argv, self.mod.checkout_launcher_path
        which = shutil.which

        def mutant_path_first():            # the PATH is consulted before the checkout's own launcher
            found = which("cloud-worker")
            return [found] if found else real_argv()

        def mutant_variable_last():         # the variable only after the checkout
            own = real_path()
            if self.mod._checkout_launcher_problem(str(own)) is None:
                return [str(own)]
            return real_argv()

        def mutant_cwd():                   # the working directory instead of the module's own location
            return Path(os.getcwd()) / "cloud" / "launcher" / "cloud-worker"

        def mutant_path_only():             # no checkout lookup at all
            given = os.environ.get("FLEET_CLOUD_WORKER_BIN")
            if given:
                return [given]
            if which("cloud-worker"):
                return ["cloud-worker"]
            raise self.mod.Refusal("none")

        for label, patch in (("path before checkout", mock.patch.object(self.mod, "launcher_argv", mutant_path_first)),
                             ("checkout before variable", mock.patch.object(self.mod, "launcher_argv", mutant_variable_last)),
                             ("working directory as the checkout", mock.patch.object(self.mod.cloud_cluster, "checkout_launcher_path", mutant_cwd)),
                             ("no checkout lookup", mock.patch.object(self.mod, "launcher_argv", mutant_path_only))):
            with self.subTest(label), patch:
                self.assertNotEqual(self.ordering_failures(), [], label)

