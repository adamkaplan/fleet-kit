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
# the places, each a function text -> list of the description lines (derived: the table row, the template comment, the launcher README row, the help words)
PLACES = {
    "quickstart settings row": lambda: row(read(QS)),
    "quickstart template": lambda: template(read(QS)),
    "launcher README row": lambda: row(read("cloud/launcher/README.md").replace("| `%s` (`CW_CLOUD_CA_FILE`)" % KEY, "| `%s`" % KEY)),
    "launch help (refusal words)": lambda: [cw.VALIDATORS[KEY][0]],
}


class TestClusterServerWording(unittest.TestCase):
    def test_every_place_found(self):
        for name, get in PLACES.items():
            self.assertEqual(len(get()), 1, name)

    def test_places_say_the_url(self):
        for name, get in PLACES.items():
            with self.subTest(name):
                self.assertIn(GOOD, get()[0])

    def test_places_do_not_call_it_a_host_name(self):
        for name, get in PLACES.items():
            with self.subTest(name):
                self.assertIsNone(BAD.search(get()[0]), get()[0])

    def test_every_doc_line_with_the_key_that_describes_it_says_the_url(self):
        # the whole family: any prose line in the docs that sets or describes the key (not a table of other settings) names the URL
        for rel in (QS, "docs/cloud-worker-interface.md", "cloud/launcher/README.md"):
            for l in read(rel).splitlines():
                if KEY in l and ("set `%s`" % KEY in l) and "CW_CLOUD_TOKEN_FILE" in l and "Turn it on" not in l and "logs in as" not in l:
                    self.assertIn(GOOD, l, rel)

    def test_window_sentence_present(self):
        self.assertIn(WINDOW, read(QS))

    # mutants: a copy of the text with the claim broken; the same check must then fail
    def test_mutants_are_caught(self):
        for name, get in PLACES.items():
            with self.subTest(name):
                mutant = get()[0].replace(GOOD, "the cluster host name")
                self.assertNotIn(GOOD, mutant)
                self.assertIsNotNone(BAD.search(mutant))
        self.assertNotIn(WINDOW, read(QS).replace("the checks guard against OTHER users", "the checks guard"))
        self.assertIsNotNone(BAD.search("| `%s` | yes | name | x |" % KEY))


if __name__ == "__main__":
    unittest.main()
