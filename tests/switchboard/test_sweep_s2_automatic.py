"""S2 of the captain sweep: a PR marked `MERGE: automatic` is not held for the person, and `needs:captain` is a lane's review label, not an owner hold."""
import types
from unittest import mock
from pathlib import Path
from support.core import Base
from support.loader import sb
from support.herdr_world import World
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE

NOW = 1_800_000_000.0
REPO = "acme/widgets"
WAITS = "This waits for the owner's review."
WAITING_BODY = "waiting on the owner"


class Hub:
    """Answers the three reads of one repo: its open list, its closed list and its recent comments."""
    def __init__(self, pr, comment):
        self.pr, self.comment = pr, comment

    def read_pages(self, path, cap=1):
        return ([self.pr] if "state=open" in path else []), False

    def read_comments(self, who, repo, since, cap=1, budget=None):
        return ([{"issue_url": "https://api.github.com/repos/%s/issues/5" % REPO, "created_at": "2099-01-01T00:00:00Z", "id": 1, "body": self.comment}] if self.comment else []), False


class TestSweepS2Automatic(Base):
    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("coder",))
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        self.engine = types.SimpleNamespace(state_dir=Path(self.world.state), now=lambda: NOW, config={}, hub=None,
                                            _note=lambda *a: None)

    def s2(self, labels, body, comment):
        cfg = dict(sb.SWEEP_DEFAULTS)
        regex = sb.sweep_phrase_re(cfg["person_names"])
        pr = {"number": 5, "pull_request": {"url": "u"}, "labels": [{"name": n} for n in labels], "body": body, "user": {"login": "cloud-pod"},
              "created_at": "2020-01-01T00:00:00Z", "updated_at": "2020-01-01T00:00:00Z"}
        state = types.SimpleNamespace(repos={}, sources={}, unattributed=0, strong=sb.sweep_strong_re(cfg["person_names"]))
        state.repos[REPO] = sb.sweep_read_repo(Hub(pr, comment), REPO, NOW, 7, regex, [50], state.strong)
        found = sb.sweep_findings(self.engine, state, [], REPO, NOW, cfg, regex)
        return [f for f in found if f[0] == "S2"], [f for f in found if f[0] == "S6b"]

    def test_a_needs_captain_review_label_with_an_automatic_line_is_not_held_for_the_person(self):
        s2, _ = self.s2(["needs:captain"], "MERGE: automatic\n\nPlan: n/a", WAITS)
        self.assertEqual(s2, [])
        s2, _ = self.s2(["needs:captain"], "\n  MERGE: automatic\nbody", None)
        self.assertEqual(s2, [])

    def test_a_captain_line_with_waiting_text_is_still_s2(self):
        with mock.patch.object(sb, "CAPTAIN_MARKER_UNTIL", None):   # the line is no longer read as a hold: the real clock must not decide the test
            s2, _ = self.s2([], "MERGE: captain\n\n" + WAITING_BODY, WAITS)
        self.assertEqual(len(s2), 1)
        s2, _ = self.s2(["needs:captain"], "MERGE: automatic\n" + WAITING_BODY, None)
        self.assertEqual(s2, [])
        s2, _ = self.s2(["needs:captain"], "Some text\nMERGE: automatic\n", WAITS)   # not the FIRST line: no automatic marker
        self.assertEqual(len(s2), 1)

    def test_no_markers_and_text_that_waits_is_s2(self):
        s2, _ = self.s2([], "A change.\n" + WAITING_BODY, None)
        self.assertEqual(len(s2), 1)
        s2, _ = self.s2([], "A change.", WAITS)
        self.assertEqual(len(s2), 1)

    def test_an_automatic_line_beside_an_owner_hold_label_is_not_exempt(self):
        s2, s6b = self.s2(["fleet:merge-captain"], "MERGE: automatic\n" + WAITING_BODY, WAITS)
        self.assertEqual(s2, [])   # captain: S2 never applies
        self.assertEqual(len(s6b), 1)   # the disagreement is S6b as before
        s2, _ = self.s2(["fleet:hold-security"], "MERGE: automatic\n" + WAITING_BODY, None)
        self.assertEqual(len(s2), 1)
