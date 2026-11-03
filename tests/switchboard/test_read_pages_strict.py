"""#1983: a page that is not a list is an error. REAL: GitHubHub.read_pages and _page_records. FAKE: the gh runner (gh_api) only."""
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from support.paths import BIN
from support.loader import copy_entry, _load, sb

PATH = "repos/o/r/issues?state=open"
CHECK = "a page that is not a list of objects is an error, never a short list"


def hub_over(pages, requests=None, module=None):
    """The REAL read_pages over a fake gh_api answering pages[number]."""
    mod = module or sb
    hub = mod.GitHubHub.__new__(mod.GitHubHub)

    def gh_api(path, paginate=True, timeout=None):
        if requests is not None:
            requests.append(path)
        return pages[int(re.search(r"[?&]page=(\d+)", path).group(1))]
    hub.gh_api = gh_api
    return hub


def full(n=100):
    return [{"number": i} for i in range(n)]


def odd_cases():
    return {"non-list page 1": {1: ["boom"]}, "non-list page 3": {1: [full()], 2: [full()], 3: ["boom"]},
            "list of strings": {1: [["a", "b"]]}, "object without the key": {1: [{"other": []}]}}


def strict_body(test, mod):
    """The S2 count on module `mod`: 4 odd pages raise GitHubUnavailable naming path and page."""
    raised = 0
    for name, pages in odd_cases().items():
        try:
            hub_over(pages, module=mod).read_pages(PATH, key="items" if "key" in name else None)
        except mod.GitHubUnavailable as exc:
            raised += PATH in str(exc) and "page %d" % max(pages) in str(exc)
    test.assertEqual(raised, 4, CHECK)


class TestReadPagesStrict(unittest.TestCase):
    def test_four_odd_pages_raise_naming_path_and_page(self):   # REAL read_pages, FAKE gh_api
        strict_body(self, sb)

    def test_normal_three_page_read_and_empty_page_return(self):   # REAL read_pages, FAKE gh_api
        requests = []
        records, cut = hub_over({1: [full()], 2: [full()], 3: [full(50)]}, requests).read_pages(PATH)
        self.assertEqual((len(records), cut), (250, False))
        self.assertEqual(len(requests), 3)   # S3: the request count is unchanged
        self.assertEqual(hub_over({1: []}).read_pages(PATH), ([], False))
        self.assertEqual(hub_over({1: [{"items": full(3)}]}).read_pages(PATH, key="items"), (full(3), False))


class TestReadPagesStrictMutant(unittest.TestCase):
    """REAL entry copied to /tmp-like dir; the mutant restores the silent drop by text; control (unmutated copy) first."""
    def load(self, name, mutate):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        for part in BIN.glob("*.py"):
            shutil.copy(str(part), str(Path(tmp, part.name)))
        copy_entry(tmp)
        path = Path(tmp, "fleet-switchboard")
        text = path.read_text()
        path.write_text(mutate(text))
        return _load(name, path)

    def test_control_then_mutant(self):
        control = self.load("t_strict_control", lambda t: t)
        strict_body(self, control)   # control first: passes
        guard = 'raise GitHubUnavailable("%s page %d is not a list of objects" % (path, number))'
        source = Path(BIN, "fleet-switchboard").read_text()
        self.assertEqual(source.count(guard), 1)
        mutant = self.load("t_strict_mutant", lambda t: t.replace(guard, "continue"))
        with self.assertRaises(AssertionError) as caught:
            strict_body(self, mutant)
        self.assertIn(CHECK, str(caught.exception))
        self.assertIn("0 != 4", str(caught.exception).split(" : ")[0])


if __name__ == "__main__":
    unittest.main()
