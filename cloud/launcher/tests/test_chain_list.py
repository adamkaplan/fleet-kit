"""Ask #1308: the chain is one entry per line (S1-S7, S9, S10, S12, U1 shapes). Offline; scratch trees under the temp dir."""
import os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SCRIPT = os.path.join(ROOT, "bin", "test-cloud-launcher")
LISTF = SCRIPT + ".chain"
FIXED_LAST = "cloud/worker/tests/test_cloud_prepush_preflight.py"


def entries():
    with open(LISTF) as f:
        return [l for l in f.read().split("\n") if l]


def slurp(path):
    with open(path) as f:
        return f.read()


def git(cwd, *a):
    return subprocess.run(("git",) + a, cwd=cwd, capture_output=True, text=True)


class Scratch(unittest.TestCase):
    def tree(self, names, tests=("test_x.py",), exceptions="", raw=None, last=True, bins=()):
        d = tempfile.mkdtemp(prefix="chain-scratch-")
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "bin")); os.makedirs(os.path.join(d, "cloud/launcher/tests"))
        with open(SCRIPT) as f:
            s = f.read()
        s = s.replace('EXCEPTIONS="', 'EXCEPTIONS="%s' % exceptions, 1) if exceptions else s
        with open(os.path.join(d, "bin", "test-cloud-launcher"), "w") as f:
            f.write(s)
        os.chmod(os.path.join(d, "bin", "test-cloud-launcher"), 0o755)
        for n in tests:
            with open(os.path.join(d, "cloud/launcher/tests", n), "w") as f:
                f.write("import sys, os\nopen(os.path.join(os.environ['CHAIN_LOG'], %r), 'a').write('x')\nsys.exit(int(%r.startswith('test_bad')))\n" % (n, n))
        os.makedirs(os.path.join(d, "cloud/worker/tests"))
        with open(os.path.join(d, FIXED_LAST), "w") as f:
            f.write("import sys, os\nopen(os.path.join(os.environ['CHAIN_LOG'], 'LAST'), 'a').write('x')\n")
        for b in bins:
            with open(os.path.join(d, "bin", b), "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(os.path.join(d, "bin", b), 0o755)
        text = "".join("cloud/launcher/tests/%s\n" % n for n in names) if raw is None else raw
        if last and raw is None:
            text += FIXED_LAST + "\n"
        with open(os.path.join(d, "bin", "test-cloud-launcher.chain"), "w") as f:
            f.write(text)
        return d

    def run_chain(self, d):
        log = tempfile.mkdtemp(prefix="chain-log-"); self.addCleanup(shutil.rmtree, log, True)
        env = dict(os.environ, CHAIN_LOG=log)
        p = subprocess.run([os.path.join(d, "bin", "test-cloud-launcher")], cwd=d, env=env, capture_output=True, text=True)
        return p, sorted(x for x in os.listdir(log) if x != 'LAST')

    def test_s2_all_pass_runs_each_once_and_prints_pass(self):
        d = self.tree(["test_a.py", "test_b.py"], tests=("test_a.py", "test_b.py"))
        p, ran = self.run_chain(d)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.strip().split("\n")[-1], "RESULT: PASS")
        self.assertEqual(ran, ["test_a.py", "test_b.py"])

    def test_s3_first_red_stops_the_chain(self):
        d = self.tree(["test_a.py", "test_bad.py", "test_c.py"], tests=("test_a.py", "test_bad.py", "test_c.py"))
        p, ran = self.run_chain(d)
        self.assertEqual(p.returncode, 1)
        self.assertIn("RESULT: FAIL", p.stdout)
        self.assertEqual(ran, ["test_a.py", "test_bad.py"])

    def test_s5_unlisted_file_keeps_the_message(self):
        d = self.tree(["test_a.py"], tests=("test_a.py", "test_x.py"))
        p, _ = self.run_chain(d)
        self.assertEqual(p.returncode, 1)
        self.assertIn("NOT RUN: test_x.py is neither chained nor in EXCEPTIONS", p.stdout)

    def test_s6_missing_entry_fails_and_names_it(self):
        d = self.tree(["test_a.py", "test_gone.py"], tests=("test_a.py",))
        p, ran = self.run_chain(d)
        self.assertEqual(p.returncode, 1)
        self.assertIn("test_gone.py", p.stdout)
        self.assertEqual(ran, [])

    def test_exception_with_reason_is_accepted(self):
        d = self.tree(["test_a.py"], tests=("test_a.py", "test_slow.py"), exceptions="test_slow.py:slow ")
        p, _ = self.run_chain(d)
        self.assertEqual(p.returncode, 0, p.stdout)


class Guards(Scratch):
    def fails(self, d, msg):
        p, ran = self.run_chain(d)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn(msg, p.stdout)
        self.assertIn("RESULT: FAIL", p.stdout)
        self.assertNotIn("RESULT: PASS", p.stdout)
        return ran

    def test_h2_empty_list_fails(self):
        self.fails(self.tree([], tests=(), raw=""), "NOT RUN: the chain list holds no entry")

    def test_h2_blank_only_list_fails(self):
        self.fails(self.tree([], tests=(), raw="\n\n  \n"), "NOT RUN: the chain list holds no entry")

    def test_h2_ran_count_is_printed(self):
        p, _ = self.run_chain(self.tree(["test_a.py"], tests=("test_a.py",)))
        self.assertIn("ran 2 entries", p.stdout)

    def test_h1_unlisted_bin_script_fails(self):
        self.fails(self.tree(["test_a.py"], tests=("test_a.py",), bins=("test-cloud-extra",)),
                   "NOT RUN: bin/test-cloud-extra is in no chain list entry and not in BIN_EXCEPTIONS")

    def test_m2_last_line_not_fixed_fails(self):
        d = self.tree([], tests=("test_a.py",), raw=FIXED_LAST + "\ncloud/launcher/tests/test_a.py\n")
        self.fails(d, "the last line of bin/test-cloud-launcher.chain must be " + FIXED_LAST)

    def test_m2_same_named_worker_test_does_not_satisfy_a_launcher_test(self):
        d = self.tree([], tests=("test_x.py",), raw="cloud/worker/tests/test_x.py\n" + FIXED_LAST + "\n")
        self.fails(d, "NOT RUN: test_x.py is neither chained nor in EXCEPTIONS")

    def test_m3_cost_script_keeps_the_old_message(self):
        d = self.tree(["test_a.py"], tests=("test_a.py",), bins=("test-cloud-cost-new",))
        self.fails(d, "NOT RUN: bin/test-cloud-cost-new is not chained")

    def test_m3_old_messages_equal_the_integration_script_byte_for_byte(self):
        old = git(ROOT, "show", "origin/integration:bin/test-cloud-launcher")
        if old.returncode != 0:
            self.skipTest("origin/integration is not available")
        new = slurp(SCRIPT)
        for lit in ('echo "NOT RUN: $n is neither chained nor in EXCEPTIONS"; echo "RESULT: FAIL"; exit 1',
                    'echo "NOT RUN: bin/$n is not chained"; echo "RESULT: FAIL"; exit 1'):
            self.assertIn(lit, old.stdout)
            self.assertIn(lit, new)

    def test_low_symlink_entry_refused(self):
        d = self.tree([], tests=())
        with open(os.path.join(d, "target.py"), "w") as f:
            f.write("pass\n")
        os.symlink(os.path.join(d, "target.py"), os.path.join(d, "cloud/launcher/tests/test_link.py"))
        with open(os.path.join(d, "bin", "test-cloud-launcher.chain"), "w") as f:
            f.write("cloud/launcher/tests/test_link.py\n" + FIXED_LAST + "\n")
        self.fails(d, "NOT RUN: entry is a symlink")

    def test_low_duplicate_entry_fails_and_runs_nothing(self):
        d = self.tree(["test_a.py", "test_a.py"], tests=("test_a.py",))
        self.assertEqual(self.fails(d, "duplicate entry cloud/launcher/tests/test_a.py"), [])

    def test_low_trailing_space_entry_is_missing(self):
        d = self.tree([], tests=(), raw="cloud/launcher/tests/test_a.py \n" + FIXED_LAST + "\n")
        self.fails(d, "does not exist")

    def test_low_entry_outside_repository_refused(self):
        for bad in ("../x.py", "/etc/passwd", "cloud/../../x.py"):
            d = self.tree([], tests=(), raw=bad + "\n" + FIXED_LAST + "\n")
            self.fails(d, "NOT RUN: entry outside the repository")

    def test_m3_arguments_only_to_old_argument_entries(self):
        d = self.tree([], tests=("test_driver.py", "test_other.py"), raw="")
        for n in ("test_driver.py", "test_other.py"):
            with open(os.path.join(d, "cloud/launcher/tests", n), "w") as f:
                f.write("import sys, os\nopen(os.path.join(os.environ['CHAIN_LOG'], %r), 'w').write(' '.join(sys.argv[1:]))\n" % n)
        with open(os.path.join(d, "bin", "test-cloud-launcher.chain"), "w") as f:
            f.write("cloud/launcher/tests/test_driver.py\ncloud/launcher/tests/test_other.py\n" + FIXED_LAST + "\n")
        log = tempfile.mkdtemp(prefix="chain-log-"); self.addCleanup(shutil.rmtree, log, True)
        p = subprocess.run([os.path.join(d, "bin", "test-cloud-launcher"), "ARGX"], cwd=d, env=dict(os.environ, CHAIN_LOG=log), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual(slurp(os.path.join(log, "test_driver.py")), "ARGX")
        self.assertEqual(slurp(os.path.join(log, "test_other.py")), "")


class RealTree(unittest.TestCase):
    def test_s7_every_test_file_listed_or_excepted_and_every_entry_exists(self):
        text = slurp(SCRIPT)
        exc = text.split("EXCEPTIONS=")[1].split("\n")[0]
        listed = entries()
        unlisted = [n for n in sorted(os.listdir(HERE)) if n.startswith("test_") and n.endswith(".py")
                    and "cloud/launcher/tests/" + n not in listed and n + ":" not in exc]
        self.assertEqual(unlisted, [])
        self.assertEqual([e for e in listed if not os.path.isfile(os.path.join(ROOT, e))], [])

    def test_h1_the_cost_report_script_is_listed_once(self):
        listed = entries()
        for b in ("bin/test-cloud-cost-report",):
            self.assertEqual(listed.count(b), 1, b)

    @staticmethod
    def uncovered(bins, listed, exc, workflow_text):
        names = [e.split(":")[0] for e in exc.split()]
        return [b for b in bins if "bin/" + b not in listed and b not in names and ("bin/" + b) not in workflow_text]

    def test_h1_every_cloud_bin_script_is_chained_excepted_or_a_workflow_step(self):
        # What the old test_h1 pinned: each relay-family script (and the cost report) is run, listed exactly once in the list.
        # This derives the set from the files: bin/test-cloud-*, the list, BIN_EXCEPTIONS, and every workflow file.
        bins = sorted(n for n in os.listdir(os.path.join(ROOT, "bin")) if n.startswith("test-cloud-") and not n.endswith(".chain"))
        exc = slurp(SCRIPT).split("BIN_EXCEPTIONS=")[1].split("\n")[0].strip('"')
        wfdir = os.path.join(ROOT, ".github", "workflows")
        wf = "".join(slurp(os.path.join(wfdir, n)) for n in sorted(os.listdir(wfdir)) if n.endswith((".yml", ".yaml")))
        listed = entries()
        self.assertGreater(len(bins), 5)
        self.assertEqual(self.uncovered(bins, listed, exc, wf), [])
        for b in ("test-cloud-hub", "test-cloud-relay", "test-cloud-hub-relay", "test-cloud-relay-daemon"):
            self.assertIn(b, bins)
            self.assertNotIn(b + ":own-ci-step", exc)
        # mutant: a new script in none of the three places is caught (assert the mutation applied)
        mutated = bins + ["test-cloud-zz-new"]
        self.assertEqual(len(mutated), len(bins) + 1)
        self.assertEqual(self.uncovered(mutated, listed, exc, wf), ["test-cloud-zz-new"])

    def test_s4_the_old_commands_are_each_one_entry(self):
        listed = entries()
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(listed[-1], FIXED_LAST)
        self.assertEqual(listed[:-1], sorted(listed[:-1]))
        self.assertGreater(len(listed), 40)

    def test_s1_s12_the_cause_and_the_how_to_are_documented(self):
        doc = slurp(os.path.join(ROOT, "cloud", "launcher", "README.md"))
        self.assertIn("ONE shell line of more than forty commands", doc)
        self.assertIn("add one line to bin/test-cloud-launcher.chain at its alphabetical place", doc)
        self.assertIn("ONE shell line of more than forty commands", slurp(SCRIPT).replace("\n# ", " "))


class Merge(unittest.TestCase):
    """S9 and S10: two scratch branches each adding a different entry merge without a conflict; the single-line form does not."""

    def merge(self, base_text, edit_a, edit_b):
        d = tempfile.mkdtemp(prefix="chain-merge-"); self.addCleanup(shutil.rmtree, d, True)
        git(d, "init", "-q"); git(d, "config", "user.name", "t"); git(d, "config", "user.email", "t@example.com")
        path = os.path.join(d, "chain")
        with open(path, "w") as f:
            f.write(base_text)
        git(d, "add", "chain"); git(d, "commit", "-qm", "base")
        base = git(d, "rev-parse", "HEAD").stdout.strip()
        for br, edit in (("a", edit_a), ("b", edit_b)):
            git(d, "checkout", "-q", "-b", br, base)
            with open(path, "w") as f:
                f.write(edit(base_text))
            git(d, "commit", "-qam", br)
        git(d, "checkout", "-q", "a")
        return git(d, "merge", "--no-edit", "b").returncode, slurp(path)

    def test_s9_real_list_form_merges_clean(self):
        base = "".join(e + "\n" for e in entries())
        add = lambda name: (lambda t: t.replace("cloud/launcher/tests/test_cloud_stack.py\n", "cloud/launcher/tests/test_cloud_stack.py\n%s\n" % name))
        # different alphabetical places, as the plan asks
        add_a = lambda t: t.replace("cloud/launcher/tests/test_app_token.py\n", "cloud/launcher/tests/test_app_token.py\ncloud/launcher/tests/test_new_a.py\n")
        add_b = lambda t: t.replace("cloud/launcher/tests/test_stub_cleanup.py\n", "cloud/launcher/tests/test_new_z.py\ncloud/launcher/tests/test_stub_cleanup.py\n")
        self.assertNotEqual(add_a(base), base); self.assertNotEqual(add_b(base), base)
        rc, merged = self.merge(base, add_a, add_b)
        self.assertEqual(rc, 0)
        self.assertIn("test_new_a.py", merged); self.assertIn("test_new_z.py", merged)

    def test_m1_adjacent_names_conflict_a_stated_limit(self):
        base = "".join(e + "\n" for e in entries())
        add_a = lambda t: t.replace("cloud/launcher/tests/test_app_token.py\n", "cloud/launcher/tests/test_app_token.py\ncloud/launcher/tests/test_new_a.py\n")
        add_b = lambda t: t.replace("cloud/launcher/tests/test_app_token.py\n", "cloud/launcher/tests/test_app_token.py\ncloud/launcher/tests/test_new_b.py\n")
        rc, _ = self.merge(base, add_a, add_b)
        self.assertNotEqual(rc, 0)  # residual risk: inserts at the same place conflict

    def test_s10_mutant_single_line_form_conflicts(self):
        self.assertGreater(len(entries()), 40)  # the real list, not a synthetic string
        base = "if " + " && ".join("python3 " + e for e in entries()) + "; then echo RESULT: PASS; fi\n"
        add_a = lambda t: t.replace("; then", " && python3 cloud/launcher/tests/test_new_a.py; then")
        add_b = lambda t: t.replace("; then", " && python3 cloud/launcher/tests/test_new_z.py; then")
        rc, _ = self.merge(base, add_a, add_b)
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
