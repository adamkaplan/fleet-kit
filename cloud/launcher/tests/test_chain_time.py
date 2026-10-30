"""Ask #1390: the chain's parallel list. Every entry still runs (the parallel list is a subset of the chain), a red parallel entry fails the run,
and a parallel entry that is not in the chain is refused. Offline; scratch trees under the temp dir."""
import os, re, subprocess, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_chain_list as base

PARALLEL = base.SCRIPT + ".parallel"

# The temporary-directory markers a parallel entry may show (checked by the scan; each is used by at least one entry). The three
# "import" markers inherit a fixture module's own TemporaryDirectory (test_cloud_worker.Base, test_pr_edit, test_app_token/test_driver).
TEMP_MARKERS = {
    "mkdtemp": r"\bmkdtemp\(",
    "TemporaryDirectory": r"\bTemporaryDirectory\(",
    "mkstemp": r"\bmkstemp\(",
    "test_cloud_worker": r"^\s*(import|from) test_cloud_worker\b",
    "test_pr_edit": r"^\s*(import|from) test_pr_edit\b",
    "test_driver": r"^\s*(import|from) test_driver\b",
}
# What a parallel entry must not do: bind a fixed port (a nonzero literal or a constant) or write a fixed absolute path.
SHARED = {
    "bind with a literal port": r"\.bind\(\s*\(?[^)]*,\s*[1-9]",
    "server on a constant or nonzero port": r"(HTTPServer|TCPServer|UDPServer)\(\s*\([^)]*,\s*(?!0\s*\))[A-Za-z0-9_]+",
    "port= with a nonzero literal": r"\bport\s*=\s*[1-9]",
    "a port constant": r"\b[A-Z_]*PORT\s*=\s*[1-9]",
    "open of a fixed absolute path for writing": r"\bopen\(\s*[\"']/[^\"']*[\"']\s*,\s*[\"'][wax]",
    "Path write to a fixed absolute path": r"Path\(\s*[\"']/[^\"']*[\"']\s*\)\.(write_text|write_bytes|open\([\"'][wax])",
}


def temp_markers(src):
    return {k for k, rx in TEMP_MARKERS.items() if re.search(rx, src, re.M)}


def shared(src):
    return [k for k, rx in SHARED.items() if re.search(rx, src, re.M)]



def parallel():
    return [l for l in base.slurp(PARALLEL).split("\n") if l]


class RealTree(unittest.TestCase):
    def test_every_parallel_entry_is_in_the_chain_once(self):
        listed, par = base.entries(), parallel()
        self.assertEqual(len(par), len(set(par)))
        self.assertGreaterEqual(len(par), 3)
        self.assertEqual([e for e in par if e not in listed], [])
        self.assertNotIn(base.FIXED_LAST, par)

    def test_every_parallel_entry_has_a_named_temporary_directory_marker_and_shares_nothing(self):
        found = {}
        for e in parallel():
            src = base.slurp(os.path.join(base.ROOT, e))
            found[e] = temp_markers(src)
            self.assertTrue(found[e], "%s shows none of %s" % (e, sorted(TEMP_MARKERS)))
            self.assertEqual(shared(src), [], e)
        self.assertEqual(set().union(*found.values()), set(TEMP_MARKERS), "every accepted marker is used by at least one entry")

    def test_mutant_a_fixed_port_bind_in_a_real_entry_fails_the_scan(self):
        for plant in ('s.bind(("127.0.0.1", 8080))', 'srv = http.server.HTTPServer(("127.0.0.1", PORT), H)',
                      'srv = http.server.HTTPServer(("", 9000), H)', 'start(port=8123)', 'PORT = 8123'):
            src = self.planted(plant)
            self.assertIn(plant, src)
            self.assertTrue(shared(src), plant)

    def test_mutant_a_fixed_absolute_path_write_in_a_real_entry_fails_the_scan(self):
        for plant in ('open("/tmp/shared-name.json", "w")', "open('/var/tmp/x', 'a')", 'Path("/tmp/x").write_text("y")'):
            src = self.planted(plant)
            self.assertIn(plant, src)
            self.assertTrue(shared(src), plant)

    def test_the_scan_leaves_a_port_zero_bind_and_a_temporary_directory_path_alone(self):
        self.assertEqual(shared('s.bind(("127.0.0.1", 0))\nHTTPServer(("127.0.0.1", 0), H)\nopen(os.path.join(d, "x"), "w")'), [])

    def planted(self, line):
        entry = [e for e in parallel() if e.endswith("test_stub_cleanup.py")][0]
        return base.slurp(os.path.join(base.ROOT, entry)) + "\n" + line + "\n"


class Parallel(base.Scratch):
    def tree(self, names, par=(), **kw):
        d = super().tree(names, tests=kw.pop("tests", tuple(names)), **kw)
        with open(os.path.join(d, "bin", "test-cloud-launcher.parallel"), "w") as f:
            f.write("".join("cloud/launcher/tests/%s\n" % n for n in par))
        return d

    def test_parallel_entries_all_run_and_the_run_passes(self):
        names = ["test_a.py", "test_b.py", "test_c.py", "test_d.py", "test_e.py"]
        p, ran = self.run_chain(self.tree(names, par=names[1:]))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(ran, names)
        self.assertIn("ran 6 entries", p.stdout)
        for n in names[1:]:   # each parallel entry's own RUN and TIME lines come whole
            self.assertIn("RUN cloud/launcher/tests/%s\n" % n, p.stdout)

    def test_mutant_a_red_parallel_entry_fails_the_run_and_is_named(self):
        names = ["test_a.py", "test_bad.py", "test_c.py"]
        p, _ = self.run_chain(self.tree(names, par=["test_bad.py"]))
        self.assertEqual(p.returncode, 1)
        self.assertIn("FAILED: cloud/launcher/tests/test_bad.py", p.stdout)
        self.assertIn("RESULT: FAIL", p.stdout)

    def test_mutant_a_parallel_entry_missing_from_the_chain_is_refused(self):
        names = ["test_a.py", "test_b.py"]
        d = self.tree(names, par=["test_b.py", "test_gone.py"])
        p, _ = self.run_chain(d)
        self.assertEqual(p.returncode, 1)
        self.assertIn("parallel entry cloud/launcher/tests/test_gone.py is not in bin/test-cloud-launcher.chain", p.stdout)

    def test_mutant_an_entry_dropped_from_the_chain_but_kept_in_the_parallel_list_is_refused(self):
        d = self.tree(["test_a.py", "test_b.py"], par=["test_b.py"], tests=("test_a.py", "test_b.py", "test_c.py"))
        with open(os.path.join(d, "bin", "test-cloud-launcher.chain"), "w") as f:
            f.write("cloud/launcher/tests/test_a.py\n" + base.FIXED_LAST + "\n")
        p, ran = self.run_chain(d)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(ran, [])


class Completeness(unittest.TestCase):
    """Every chain entry is a row in BOTH tables or is named in the 'Not yet measured' line (and never both)."""
    @staticmethod
    def line_idx(sec):
        return [i for i, l in enumerate(sec.split("\n")) if l.startswith("Not yet measured:")]

    @classmethod
    def with_line(cls, sec, value):
        """A scratch copy of the section whose one real line reads value, whatever it read before."""
        ls = sec.split("\n")
        ls[cls.line_idx(sec)[0]] = value
        return "\n".join(ls)

    @classmethod
    def with_copy(cls, sec):
        """A scratch copy with the real line repeated right after itself (same content)."""
        ls = sec.split("\n")
        i = cls.line_idx(sec)[0]
        ls.insert(i + 1, ls[i])
        return "\n".join(ls)

    @staticmethod
    def parts(sec):
        mac, ub = sec.split("### macOS (per entry)")[1].split("### Ubuntu (per entry)")
        rows = [{r.split("`")[1] for r in t.split("\n") if r.startswith("| `")} for t in (mac, ub)]
        lines = [sec.split("\n")[i] for i in Completeness.line_idx(sec)]
        assert len(lines) == 1, "the section needs exactly one 'Not yet measured:' line"
        rest = lines[0].split(":", 1)[1].strip().rstrip(".")
        return rows[0], rows[1], ([] if rest == "none" else re.findall(r"`([^`]+)`", rest))

    @classmethod
    def problems(cls, sec, chain):
        mac, ub, nym = cls.parts(sec)
        chain, out = set(chain), []
        if mac != ub:
            out.append("the two tables differ: %s" % sorted(mac ^ ub))
        for e in sorted(mac | ub | set(nym)):
            if e not in chain:
                out.append("%s is not in the chain" % e)
        for e in sorted(set(nym).intersection(mac | ub)):
            out.append("%s is in a table and in the Not yet measured line" % e)
        for e in sorted(chain - mac - ub - set(nym)):
            out.append("%s is neither a row nor named as not yet measured" % e)
        return out

    def real(self):
        doc = base.slurp(os.path.join(base.ROOT, "cloud", "launcher", "README.md"))
        return doc.split("## Chain times (ask #1390)")[1].split("\n## ")[0]

    def test_s9_the_real_section_covers_every_chain_entry_once(self):
        self.assertEqual(self.problems(self.real(), base.entries()), [])

    def _mutant(self, fn, expect):
        sec = self.real()
        out = fn(sec)
        self.assertNotEqual(out, sec, "the mutation did not apply")
        got = self.problems(out, base.entries())
        self.assertTrue(any(expect in g for g in got), got)

    def test_s10_mutant_an_entry_removed_from_the_tables_and_not_named_is_refused(self):
        e = sorted(base.entries())[0]
        self._mutant(lambda s: "\n".join(l for l in s.split("\n") if not l.startswith("| `%s`" % e)), e)

    def test_s10_mutant_an_entry_in_a_table_and_in_the_line_is_refused(self):
        e = sorted(base.entries())[0]
        self._mutant(lambda s: self.with_line(s, "Not yet measured: `%s`" % e), e)

    def test_s10_mutant_a_name_in_the_line_that_is_not_in_the_chain_is_refused(self):
        self._mutant(lambda s: self.with_line(s, "Not yet measured: `cloud/launcher/tests/test_gone.py`"), "test_gone.py")
    # The code's rule: not exactly one 'Not yet measured:' line is an AssertionError naming the line (neither "none" nor first-only nor a union).
    def test_s11_mutant_a_section_without_the_line_fails_naming_the_line(self):
        sec = self.real()
        out = "\n".join(l for l in sec.split("\n") if not l.startswith("Not yet measured:"))
        self.assertNotEqual(out, sec, "the mutation did not apply")
        with self.assertRaisesRegex(AssertionError, "exactly one 'Not yet measured:' line"):
            self.problems(out, base.entries())

    def test_s11_mutant_a_section_with_two_lines_fails_naming_the_line_and_is_not_first_only_or_a_union(self):
        sec = self.real()
        out = self.with_copy(sec)
        self.assertNotEqual(out, sec, "the mutation did not apply")
        self.assertEqual(len(self.line_idx(out)), 2)
        with self.assertRaisesRegex(AssertionError, "exactly one 'Not yet measured:' line"):
            self.problems(out, base.entries())

    def test_s12_both_line_mutants_fail_and_the_parser_accepts_any_one_line_value(self):
        sec = self.real()
        e = sorted(base.entries())[:2]
        values = ["Not yet measured: none", "Not yet measured: `%s`" % e[0], "Not yet measured: `%s`, `%s`" % (e[0], e[1])]
        for v in values:
            scratch = self.with_line(sec, v)
            self.assertEqual(len(self.line_idx(scratch)), 1)
            self.assertEqual(self.with_line(scratch, v), scratch)
            self.assertEqual(self.parts(scratch)[2], re.findall(r"`([^`]+)`", v))
            two = self.with_copy(scratch)
            self.assertNotEqual(two, scratch, "the mutation did not apply")
            self.assertEqual(len(self.line_idx(two)), 2)
            none = "\n".join(l for l in scratch.split("\n") if not l.startswith("Not yet measured:"))
            self.assertNotEqual(none, scratch, "the mutation did not apply")
            for bad in (two, none):
                with self.assertRaisesRegex(AssertionError, "exactly one 'Not yet measured:' line"):
                    self.problems(bad, base.entries())


class Docs(unittest.TestCase):
    def section(self):
        doc = base.slurp(os.path.join(base.ROOT, "cloud", "launcher", "README.md"))
        return doc.split("## Chain times (ask #1390)")[1].split("\n## ")[0]

    def test_the_chain_times_section_states_the_cause_and_has_a_row_per_entry_for_both_systems(self):
        sec = self.section()
        self.assertLess(sec.index("Cause:"), sec.index("### macOS"))
        mac, ub = sec.split("### macOS (per entry)")[1].split("### Ubuntu (per entry)")
        for table in (mac, ub):
            rows = [l for l in table.split("\n") if l.startswith("| `")]
            self.assertGreaterEqual(len(rows), 40)
            names = {r.split("`")[1] for r in rows}
            self.assertLessEqual(names, set(base.entries()))
        mac_rows, ub_rows, not_yet = Completeness.parts(sec)
        self.assertEqual(mac_rows | set(not_yet), set(base.entries()))
        self.assertEqual(ub_rows | set(not_yet), set(base.entries()))
        self.assertFalse((mac_rows | ub_rows).intersection(not_yet))

    def test_the_five_slowest_per_system_are_named_and_match_the_tables(self):
        sec = self.section()
        for head, marker in (("Slowest five on macOS: ", "### macOS"), ("Slowest five on Ubuntu: ", "### Ubuntu")):
            names = [n for n in sec.split(head)[1].split("\n")[0].split("`")[1::2]]
            self.assertEqual(len(names), 5)
            table = sec.split(marker)[1].split("\n### ")[0]
            secs = {}
            for r in table.split("\n"):
                if r.startswith("| `"):
                    c = r.split("|")
                    secs[c[1].strip().strip("`").split("/")[-1]] = int(c[2])
            self.assertEqual(set(names), {n for n, _ in sorted(secs.items(), key=lambda x: -x[1])[:5]})

    def test_the_document_says_which_entries_run_where_and_names_the_parallel_ones_as_lanes(self):
        sec = self.section()
        self.assertIn("Every chain entry still runs on BOTH systems", sec)
        for e in parallel():
            self.assertRegex(sec, r"\| `%s` \| \d+ \| parallel \|" % e.replace(".", r"\."))


if __name__ == "__main__":
    unittest.main()
