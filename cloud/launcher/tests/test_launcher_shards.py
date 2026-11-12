"""Ask #2500: the cloud launcher chain runs as CI shards that together cover every chain entry exactly once. Offline; scratch trees under the temp dir.

S2: the shards of the real chain file join to exactly its entries, none twice. S3: the coverage check (bin/check-launcher-shards) fails naming a file that is in no
job or in two. U1: the shard option leaves the unsharded run as it was. The proposed workflow patch (ci/proposed/test.yml.patch) is pinned while it is waiting to be applied."""
import importlib.machinery, importlib.util, os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SCRIPT = os.path.join(ROOT, "bin", "test-cloud-launcher")
CHECK = os.path.join(ROOT, "bin", "check-launcher-shards")
PATCH = os.path.join(ROOT, "ci", "proposed", "test.yml.patch")
FIXED_LAST = "cloud/worker/tests/test_cloud_prepush_preflight.py"


def load_check():
    spec = importlib.util.spec_from_file_location("check_launcher_shards", CHECK, loader=importlib.machinery.SourceFileLoader("check_launcher_shards", CHECK))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def chain():
    with open(os.path.join(ROOT, "bin", "test-cloud-launcher.chain")) as f:
        return [l for l in f.read().split("\n") if l.strip()]


def listing(k, n):
    p = subprocess.run([SCRIPT, "--shard", "%d/%d" % (k, n), "--list"], capture_output=True, text=True, cwd=ROOT)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout.split("\n")[:-1]


class Shards(unittest.TestCase):
    def test_s2_the_shards_of_the_real_chain_join_to_its_entries_with_none_twice(self):
        entries = chain()
        for n in (1, 2, 3, 4, 5, 8, len(entries) + 3):
            joined = [e for k in range(1, n + 1) for e in listing(k, n)]
            with self.subTest(n=n):
                self.assertEqual(len(joined) - len(set(joined)), 0, "entries run twice")
                self.assertEqual(sorted(set(entries) - set(joined)) + sorted(set(joined) - set(entries)), [], "entries missing or invented")

    def test_s2_the_shards_are_balanced_to_within_one_entry(self):
        sizes = [len(listing(k, 4)) for k in range(1, 5)]
        self.assertLessEqual(max(sizes) - min(sizes), 1)

    def test_s2_a_bad_shard_argument_is_refused_without_running_anything(self):
        for arg in ("0/4", "5/4", "1/0", "x", "1", "1/4/2", "-1/4", ""):
            with self.subTest(arg=arg):
                p = subprocess.run([SCRIPT, "--shard", arg], capture_output=True, text=True, cwd=ROOT, timeout=60)
                self.assertEqual(p.returncode, 1, p.stdout)
                self.assertIn("NOT RUN: --shard", p.stdout)
                self.assertNotIn("RUN cloud", p.stdout)

    def tree(self, names):
        d = tempfile.mkdtemp(prefix="shard-scratch-")
        self.addCleanup(shutil.rmtree, d, True)
        for sub in ("bin", "cloud/launcher/tests", "cloud/worker/tests"):
            os.makedirs(os.path.join(d, sub))
        shutil.copy(SCRIPT, os.path.join(d, "bin", "test-cloud-launcher"))
        for n in names:
            with open(os.path.join(d, "cloud/launcher/tests", n), "w") as f:
                f.write("import os\nopen(os.path.join(os.environ['CHAIN_LOG'], %r), 'a').write('x')\n" % n)
        with open(os.path.join(d, FIXED_LAST), "w") as f:
            f.write("import os\nopen(os.path.join(os.environ['CHAIN_LOG'], 'LAST'), 'a').write('x')\n")
        with open(os.path.join(d, "bin", "test-cloud-launcher.chain"), "w") as f:
            f.write("".join("cloud/launcher/tests/%s\n" % n for n in names) + FIXED_LAST + "\n")
        with open(os.path.join(d, "bin", "test-cloud-launcher.parallel"), "w") as f:
            f.write("cloud/launcher/tests/%s\n" % names[1])
        return d

    def run_shard(self, d, spec):
        log = tempfile.mkdtemp(prefix="shard-log-")
        self.addCleanup(shutil.rmtree, log, True)
        args = [] if spec is None else ["--shard", spec]
        p = subprocess.run([os.path.join(d, "bin", "test-cloud-launcher")] + args, cwd=d, env=dict(os.environ, CHAIN_LOG=log), capture_output=True, text=True, timeout=120)
        return p, sorted(os.listdir(log))

    def test_s2_u1_a_shard_run_runs_only_its_entries_and_the_two_shards_run_every_entry_once(self):
        names = ["test_a.py", "test_b.py", "test_c.py", "test_d.py", "test_e.py"]
        d = self.tree(names)
        whole, ran_whole = self.run_shard(d, None)
        self.assertEqual(whole.returncode, 0, whole.stdout)
        self.assertEqual(ran_whole, sorted(names + ["LAST"]))   # unsharded: as before, every entry
        a, ran_a = self.run_shard(d, "1/2")
        b, ran_b = self.run_shard(d, "2/2")
        for p in (a, b):
            self.assertEqual(p.returncode, 0, p.stdout)
            self.assertEqual(p.stdout.strip().split("\n")[-1], "RESULT: PASS")
        self.assertEqual(set(ran_a) & set(ran_b), set())   # none twice (the parallel entry test_b.py included)
        self.assertEqual(sorted(ran_a + ran_b), sorted(names + ["LAST"]))   # none dropped
        self.assertIn("ran 3 entries", a.stdout)   # positions 1, 3, 5 of the 6 entries
        self.assertIn("test_b.py", ran_b)

    def test_s2_the_environment_variable_picks_the_shard_and_an_option_wins_over_it(self):
        d = self.tree(["test_a.py", "test_b.py", "test_c.py"])
        log = tempfile.mkdtemp(prefix="shard-log-"); self.addCleanup(shutil.rmtree, log, True)
        run = lambda *a, **e: subprocess.run([os.path.join(d, "bin", "test-cloud-launcher")] + list(a), cwd=d, env=dict(os.environ, CHAIN_LOG=log, **e), capture_output=True, text=True)
        self.assertIn("ran 2 entries", run(CLOUD_LAUNCHER_SHARD="1/2").stdout)   # positions 1 and 3 of 4
        self.assertIn("ran 2 entries", run("--shard", "2/2", CLOUD_LAUNCHER_SHARD="1/2").stdout)
        self.assertIn("NOT RUN: --shard", run(CLOUD_LAUNCHER_SHARD="9/2").stdout)

    def test_s2_a_red_entry_of_a_shard_still_fails_that_shard(self):
        d = self.tree(["test_a.py", "test_b.py", "test_c.py"])
        with open(os.path.join(d, "cloud/launcher/tests/test_a.py"), "w") as f:
            f.write("import sys\nsys.exit(1)\n")
        p, _ = self.run_shard(d, "1/2")
        self.assertEqual(p.returncode, 1)
        self.assertIn("RESULT: FAIL", p.stdout)
        q, _ = self.run_shard(d, "2/2")
        self.assertEqual(q.returncode, 0)   # the red entry is in shard 1 only


class Coverage(unittest.TestCase):
    def setUp(self):
        self.mod = load_check()

    def test_s3_a_group_missing_from_the_jobs_fails_naming_the_files(self):
        entries = chain()
        got = self.mod.problems(entries, 4, [1, 2, 3], lambda k: listing(k, 4))
        self.assertEqual(sorted(g[len("in no job: "):] for g in got), sorted(listing(4, 4)))
        self.assertTrue(all(g.startswith("in no job: ") for g in got))

    def test_s3_a_group_run_twice_fails_naming_the_files(self):
        got = self.mod.problems(chain(), 4, [1, 2, 3, 4, 2], lambda k: listing(k, 4))
        self.assertEqual(sorted(g[len("in 2 jobs: "):] for g in got), sorted(listing(2, 4)))

    def test_s3_a_file_in_no_group_at_all_is_named(self):
        entries = chain()
        dropped = entries[7]
        got = self.mod.problems(entries, 2, [1, 2], lambda k: [e for e in listing(k, 2) if e != dropped])
        self.assertEqual(got, ["in no job: " + dropped])

    def test_s3_every_group_once_passes(self):
        self.assertEqual(self.mod.problems(chain(), 4, [1, 2, 3, 4], lambda k: listing(k, 4)), [])

    def test_s3_a_group_number_outside_the_count_fails(self):
        self.assertTrue(self.mod.problems(chain(), 4, [1, 2, 3, 4, 5], lambda k: listing(k, 4) if k <= 4 else []))

    def cli(self, *args):
        return subprocess.run([sys.executable, CHECK] + list(args), capture_output=True, text=True, cwd=ROOT)

    def test_s3_the_command_fails_with_status_1_and_names_a_file_in_no_job(self):
        p = self.cli("--of", "4", "--ids", "1,2,3")
        self.assertEqual(p.returncode, 1)
        self.assertIn("in no job: " + listing(4, 4)[0], p.stdout)
        self.assertEqual(p.stdout.strip().split("\n")[-1], "RESULT: FAIL")
        self.assertEqual(self.cli("--of", "4", "--ids", "1,2,3,4").returncode, 0)

    def workflow(self, ids, n=4, extra=""):
        d = tempfile.mkdtemp(prefix="wf-")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "test.yml")
        with open(path, "w") as f:
            f.write("jobs:\n  other:\n    steps:\n      - run: echo x\n  launcher:\n    strategy:\n      matrix:\n        shard: %s\n    steps:\n"
                    "      - run: |\n          bin/test-cloud-launcher --shard ${{ matrix.shard }}/%d 2>&1 | tee out.txt\n%s" % (ids, n, extra))
        return path

    def test_s3_the_workflow_matrix_is_read_and_a_dropped_shard_fails(self):
        self.assertEqual(self.cli("--workflow", self.workflow("[1, 2, 3, 4]")).returncode, 0)
        p = self.cli("--workflow", self.workflow("[1, 2, 4]"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("in no job: " + listing(3, 4)[0], p.stdout)
        q = self.cli("--workflow", self.workflow("[1, 2, 3, 4, 4]"))
        self.assertEqual(q.returncode, 1)
        self.assertIn("in 2 jobs: " + listing(4, 4)[0], q.stdout)

    def test_s3_the_environment_form_of_the_workflow_is_read_too(self):
        d = tempfile.mkdtemp(prefix="wf-"); self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "test.yml")
        text = ("jobs:\n  launcher:\n    strategy:\n      matrix:\n        shard: [%s]\n    steps:\n      - env:\n          CLOUD_LAUNCHER_SHARD: ${{ matrix.shard }}/4\n"
                "        run: |\n          bin/test-cloud-launcher 2>&1 | tee out.txt\n")
        with open(path, "w") as f:
            f.write(text % "1, 2, 3, 4")
        self.assertEqual(self.cli("--workflow", path).returncode, 0)
        with open(path, "w") as f:
            f.write(text % "1, 2, 3")
        p = self.cli("--workflow", path)
        self.assertEqual(p.returncode, 1)
        self.assertIn("in no job: " + listing(4, 4)[0], p.stdout)

    def test_s3_the_shards_made_by_the_plan_rows_are_read_and_a_dropped_one_fails(self):
        d = tempfile.mkdtemp(prefix="wf-"); self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "test.yml")
        text = ("jobs:\n  plan:\n    steps:\n      - run: |\n          python3 - <<'PY'\n          out.write(\"launcher=\" + json.dumps([dict(r, shard=k) for r in rows for k in range(1, %d)]))\n          PY\n"
                "  launcher:\n    steps:\n      - env:\n          CLOUD_LAUNCHER_SHARD: ${{ matrix.shard }}/4\n        run: |\n          bin/test-cloud-launcher 2>&1 | tee out.txt\n")
        with open(path, "w") as f:
            f.write(text % 5)
        self.assertEqual(self.cli("--workflow", path).returncode, 0)
        with open(path, "w") as f:
            f.write(text % 4)
        p = self.cli("--workflow", path)
        self.assertEqual(p.returncode, 1)
        self.assertIn("in no job: " + listing(4, 4)[0], p.stdout)

    def test_s3_a_second_step_with_a_fixed_shard_counts_as_a_second_job(self):
        extra = "      - run: bin/test-cloud-launcher --shard 1/4\n"
        p = self.cli("--workflow", self.workflow("[1, 2, 3, 4]", extra=extra))
        self.assertEqual(p.returncode, 1)
        self.assertIn("in 2 jobs: " + listing(1, 4)[0], p.stdout)

    def test_s3_the_real_workflow_passes_whether_the_patch_is_applied_or_not(self):
        p = self.cli()
        self.assertEqual(p.returncode, 0, p.stdout)


class Patch(unittest.TestCase):
    """The workflow file cannot be pushed by the pod's token: ci/proposed/test.yml.patch waits for the owner. It must apply (or be applied already)."""

    def git_apply(self, *flags):
        return subprocess.run(["git", "apply", "--check"] + list(flags) + [PATCH], cwd=ROOT, capture_output=True, text=True).returncode == 0

    def test_the_patch_applies_to_the_workflow_or_is_already_applied(self):
        if not os.path.exists(PATCH):
            self.skipTest("the patch was applied and its file removed")
        self.assertTrue(self.git_apply() or self.git_apply("-R"), "ci/proposed/test.yml.patch neither applies nor reverses cleanly")

    def test_the_patch_carries_the_split_the_limit_and_the_node_step(self):
        if not os.path.exists(PATCH):
            self.skipTest("the patch was applied and its file removed")
        with open(PATCH) as f:
            text = f.read()
        self.assertIn("+  test-cloud-launcher:", text)                                   # (a) the split into a job of shards
        self.assertIn("+        include: ${{ fromJSON(needs.plan.outputs.launcher) }}", text)
        self.assertIn("-        timeout-minutes: 20", text)                               # (b) the limit 20 -> 45
        self.assertIn("+        timeout-minutes: 45", text)
        self.assertIn("-      - name: bin/test-cloud-launcher\n", text)                   # the whole-suite step is gone
        self.assertIn("alarm.test.mjs plugins/fleet-decisions-tui/click.test.mjs", text)  # (c) the node step runs alarm.test.mjs
        self.assertIn("-exempt plugins/fleet-decisions-tui/alarm.test.mjs", text)         # the exempt row leaves in the same patch
        self.assertIn("+run plugins/fleet-decisions-tui/alarm.test.mjs | node --test", text)


if __name__ == "__main__":
    unittest.main()
