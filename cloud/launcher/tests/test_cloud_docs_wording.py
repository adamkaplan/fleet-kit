"""The cluster server setting is documented as the URL it is (S19): every place that describes CW_CLOUD_SERVER says
"an https URL whose host is a DNS name", none calls it a host name, and the quickstart states the launcher lookup window as a limit.

Each check runs on text; a mutant is a copy of the text in memory, never a file."""
import os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw

KEY = "CW_CLOUD_SERVER"
GOOD = "an https URL whose host is a DNS name"
BAD = re.compile(r"host ?name|bare name|\bname\b(?= \|)|cluster API address", re.I)
WINDOW = ("Limit: the checks judge the launcher file once, just before it runs. A process running as the same user can still replace the file "
          "in that window, or edit the switchboard itself: the checks guard against OTHER users, not against yourself.")


def says_url(text):
    return GOOD in text


def calls_it_a_host_name(text):
    return BAD.search(text) is not None


def has_window(text):
    return WINDOW in text


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def row(text):
    """The settings table row of the key (the line that starts with | `CW_CLOUD_SERVER`)."""
    return [l for l in text.splitlines() if l.startswith("| `%s`" % KEY)]


def template(text):
    """The comment line above KEY=value in the template block."""
    lines = text.splitlines()
    return [lines[i - 1] for i, l in enumerate(lines) if l.startswith(KEY + "=")]


QS = "docs/cloud-operator-quickstart.md"
# the doc's name for a kind the code labels differently: the reader's kind label for the key stays "name", the doc calls it what it is
DOC_KIND = {KEY: "https URL"}
# the places, each a function () -> the description lines (derived: the table row, the template comment, the launcher README row, the help words)
PLACES = {
    "quickstart settings row": lambda: row(read(QS)),
    "quickstart template": lambda: template(read(QS)),
    "launcher README row": lambda: row(read("cloud/launcher/README.md")),
    "launch help (refusal words)": lambda: [cw.VALIDATORS[KEY][0]],
}
PROSE = ("docs/cloud-worker-interface.md",)


def prose_lines(rel):
    return [l for l in read(rel).splitlines() if "set `%s` (" % KEY in l or "set `%s`" % KEY in l and "TOKEN_FILE" in l and "Turn it on" not in l]


class TestClusterServerWording(unittest.TestCase):
    def test_every_place_found(self):
        for name, get in PLACES.items():
            self.assertEqual(len(get()), 1, name)
        for rel in PROSE:
            self.assertTrue(prose_lines(rel), rel)

    def test_places_say_the_url(self):
        for name, get in PLACES.items():
            with self.subTest(name):
                self.assertTrue(says_url(get()[0]))
        for rel in PROSE:
            for l in prose_lines(rel):
                self.assertTrue(says_url(l), rel)

    def test_places_do_not_call_it_a_host_name(self):
        for name, get in PLACES.items():
            with self.subTest(name):
                self.assertFalse(calls_it_a_host_name(get()[0]), get()[0])

    def test_doc_kinds_come_from_the_code(self):
        # the quickstart's kind list is the code's kinds, with the doc's own name where DOC_KIND says so
        text = read(QS)
        m = re.search(r"its kind \(([^)]*)\):", text)
        self.assertTrue(m)
        listed = [k.strip() for k in m.group(1).split(",")]
        expected = {DOC_KIND.get(n, k) for n, k in cw.SETTING_KINDS.items()} | set(DOC_KIND.values())
        self.assertEqual(set(listed), expected)
        self.assertIn("| %s |" % DOC_KIND[KEY], row(text)[0])

    def test_window_sentence_present(self):
        self.assertTrue(has_window(read(QS)))

    # mutants: a copy of the text with the claim broken; the REAL check must then fail
    def test_mutants_are_caught(self):
        for name, get in PLACES.items():
            with self.subTest(name):
                mutant = get()[0].replace(GOOD, "the cluster host name")
                self.assertFalse(says_url(mutant))
                self.assertTrue(calls_it_a_host_name(mutant))
        qs = read(QS)
        self.assertFalse(has_window(qs.replace(WINDOW, "")))
        self.assertFalse(has_window(qs.replace("guard against OTHER users", "guard")))
        self.assertTrue(calls_it_a_host_name(row(qs)[0].replace("| https URL |", "| name |")))


if __name__ == "__main__":
    unittest.main()
