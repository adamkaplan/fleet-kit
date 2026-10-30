"""#1457: the classes that read the set of files under bin/ are all rows of the table `preflight-pr` runs on a new bin file (moved out of bin/test-switchboard, text unchanged)."""
import ast
import re
import unittest

from support.loader import sb
from support.paths import BIN, REPO


class TestTheBinSetClassesAreAllInTheTable(unittest.TestCase):
    """#1457: the classes that read the set of files under bin/ are derived from the test files themselves (a class that lists bin/ with glob, rglob, iterdir or the reader scan);
    each is a row of path_suites.NEW_BIN_GUARDS (what `preflight-pr` runs when a change adds a file under bin/) or is named here exempt, with the reason.
    A class reading bin/ by an idiom that neither READS nor READS_MORE matches and that is not hand-listed is NOT found; the hand list records known misses, it is not proof of completeness."""
    READS = re.compile(r"(?:\bBIN\b(?:\.parent)?|\(\s*\w+\s*/\s*[\"']bin[\"']\s*\)|\bbin_dir\b)\s*\.\s*(?:glob|rglob|iterdir)\(|os\.listdir\([^)]*bin|[\"']bin[\"']\s*\)\s*\.\s*(?:glob|rglob|iterdir)|\bscan\(self\.BIN\)")
    READS_MORE = re.compile(r"os\.walk\([^)]*\bbin\b|ls-files[\"'],\s*[\"']bin[\"']|\bglob\.glob\([^)]*\bbin\b")   # the os.walk, glob.glob and git ls-files idioms
    HAND_LISTED = {   # rows of the table the derivation cannot see (the class reaches the bin/ listing through a helper or an import), each named with the reason
        "bin/check-public-names": "a script, not a test class: it scans the added lines under bin/ and has no class for the derivation to find",
        "TestArchitectureDocument": "reads the bin/ modules through the architecture tools, not a spelling the derivation matches (it failed on the new module of 1429)",
        "TestRecordingCommands": "reaches the bin/ listing through switchboard_source() (tests/switchboard/support/loader.py, the SWITCHBOARD_SOURCE_FILES manifest), which the derivation does not read; the reader of PR 1489 removed its row and the test still passed",
        "TestTheReaperOnlyInstall": "reads the bin/ files through sb.Installer (built on REPO, the install checks of the TheReaperOnlyInstall base class), not a spelling the derivation matches",
        "TestDerivedClosure": "lives in tests/switchboard/test_closure.py and reaches the bin/ listing through an import, not a spelling the derivation matches",
    }
    EXEMPT = {   # each ran green against a tree with one planted new file under bin/ (an unlisted module); the reason says what it does with the listing
        "TestSlice3Modules": "walks bin/*.py to check the rules of each module's own source, and passed with a planted unlisted module",
        "TestModuleLoader": "loads each module of bin/ to prove it imports, and passed with a planted unlisted module",
        "TestWhoCanClear": "walks the tree for the files that name the clear's authority checks, and passed with a planted unlisted module",
        "TestOwnerClearModule": "reads every module's text for the owner-clear marker, and passed with a planted unlisted module",
        "TestAdoption": "reads every module's text for the adoption words, and passed with a planted unlisted module",
        "TestEveryViewDerivesFromTheRegistry": "lists bin/ for the views a command registers, and passed with a planted unlisted module",
        "TestAssembler": "copies the sibling modules the entry loads into a fixture, and passed with a planted unlisted module",
        "TestOwnMarkerNotHonoured": "reads every module's text for the marker, and passed with a planted unlisted module",
        "TestLatestRunOfWorkflow": "copies the bin/ files into a fixture tree, and passed with a planted unlisted module",
        "TestArchTools": "copies the sibling modules into a fixture tree for the architecture tools, and passed with a planted unlisted module",
        "HubFilesUnchanged": "lists cloud/worker/bin (the derivation matches its spelling), not bin/: it compares each worker copy with the file of the same name in bin/, so a new file under bin/ with no copy is never read",
        "TestGitShim": "lists bin/test-* for the suites that must route git through the shim; a new bin file that is not a test-* file is not read",
        "TestConflictCarry": "lists the modules of bin/ whose names contain clear, hold or floor to compare them with its carried table; a name outside that pattern is not read (the classes could not run in the pod: the git wrapper refuses their setUp; named so)",
    }
    FAILED_ON_1429 = ("TestArchitectureDocument", "TestAuthorityModulesGuard", "TestImpactHold", "TestRecordJsonReaders", "TestTheReaperOnlyInstall", "TestRecordingCommands", "TestDerivedClosure")

    @classmethod
    def reading_classes(cls, name, text):
        """{class name} of the top-level classes of `text` whose source lists the bin/ directory's files."""
        tree = ast.parse(text)
        return {n.name for n in tree.body if isinstance(n, ast.ClassDef) and n.name != cls.__name__ and (cls.READS.search(ast.get_source_segment(text, n) or "") or cls.READS_MORE.search(ast.get_source_segment(text, n) or ""))}

    @classmethod
    def derived(cls):
        """Computed once per class (the scan of every test file takes about a minute): the same dict is returned to every test, which must not change it."""
        if "_derived" not in cls.__dict__:
            cls._derived = cls._scan()
        return cls._derived

    @classmethod
    def _scan(cls):
        found = {}
        for path in [BIN / "test-switchboard"] + sorted((REPO / "tests" / "switchboard").glob("test_*.py")):
            for name in cls.reading_classes(path.name, path.read_text(errors="replace")):
                found[name] = path.name
        return found

    def table(self):
        return {g[2] for g in sb.load_module("path_suites").NEW_BIN_GUARDS if g[2]}

    def rows(self):
        return {g[2] or g[1] for g in sb.load_module("path_suites").NEW_BIN_GUARDS}

    def uncovered(self, found, rows, hand=None):
        """Rows that are neither derived nor hand-listed, then hand-listed names that are no row (a stale entry)."""
        hand = self.HAND_LISTED if hand is None else hand
        return sorted(r for r in rows if r not in found and r not in hand) + sorted(h for h in hand if h not in rows)

    def unlisted(self, found, table):
        return sorted(name for name in found if name not in table and name not in self.EXEMPT)

    def test_every_class_that_reads_the_bin_files_is_in_the_table_or_exempt_with_a_reason(self):
        found = self.derived()
        self.assertGreater(len(found), 5, "the derivation found no classes: the pattern no longer matches the test files")
        self.assertEqual(self.unlisted(found, self.table()), [])
        for name, why in self.EXEMPT.items():
            self.assertTrue(why.strip(), name)

    def test_an_exemption_names_a_class_that_still_reads_the_bin_files_and_is_not_also_in_the_table(self):
        found = self.derived()
        self.assertEqual(sorted(set(self.EXEMPT) - set(found)), [], "an exempt class that no longer reads bin/ is a stale exemption")
        self.assertEqual(sorted(set(self.EXEMPT) & self.table()), [])

    def test_the_classes_that_failed_on_the_new_module_of_1429_are_all_in_the_table(self):
        self.assertEqual(sorted(set(self.FAILED_ON_1429) - {n for n in self.table()}), [])

    def test_a_class_planted_in_a_copy_of_the_test_file_that_reads_the_bin_files_is_named(self):
        planted = "import unittest\nBIN = None\n\nclass TestPlantedReader(unittest.TestCase):\n    def test_it(self):\n        self.assertEqual(sorted(p.name for p in BIN.glob('*.py')), [])\n"
        found = {n: "planted" for n in self.reading_classes("planted", planted)}
        self.assertEqual(self.unlisted(found, self.table()), ["TestPlantedReader"])

    def test_every_row_of_the_table_is_derived_or_hand_listed_and_every_hand_listed_name_is_a_row(self):
        self.assertEqual(self.uncovered(self.derived(), self.rows()), [])
        for name, why in self.HAND_LISTED.items():
            self.assertGreaterEqual(len(why.strip()), 20, name)

    def test_removing_any_one_row_fails_the_check_naming_it_derived_or_hand_listed(self):
        found = self.derived()
        rows = self.rows()
        self.assertTrue(set(found) & rows and set(self.HAND_LISTED) & rows, "both kinds of row are exercised")
        for gone in sorted(rows):
            hand_only = gone in self.HAND_LISTED
            # a hand-listed row removed leaves a stale entry; a derived row removed is caught by the derived-vs-table check
            if hand_only:
                self.assertEqual(self.uncovered(found, rows - {gone}), [gone])
            else:
                self.assertEqual(self.unlisted(found, {r for r in rows if r != gone}), [gone] if gone not in self.EXEMPT else [])

    def test_a_stale_hand_listed_name_and_an_unlisted_row_both_fail_the_check(self):
        """Mutants: a hand-list entry for a class that is no row, and a row that is neither derived nor hand-listed."""
        self.assertEqual(self.uncovered(self.derived(), self.rows(), dict(self.HAND_LISTED, TestNoSuchClass="a stale entry that names no row")), ["TestNoSuchClass"])
        self.assertEqual(self.uncovered(self.derived(), self.rows() | {"TestNewRow"}), ["TestNewRow"])

    def test_the_table_with_one_class_removed_fails_the_completeness_check(self):
        found = self.derived()
        table = self.table()
        for gone in sorted(table & set(found)):
            self.assertEqual(self.unlisted(found, table - {gone}), [gone])
