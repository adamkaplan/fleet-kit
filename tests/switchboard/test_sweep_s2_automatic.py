"""S2 of the captain sweep: a PR marked `MERGE: automatic` is not held for the person, and `needs:captain` is a lane's review label, not an owner hold."""
import types
from unittest import mock
from pathlib import Path
from support.core import Base
from support.loader import sb
from support.herdr_world import World
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE

NOW = 1_800_000_000.0   # 2027-01-15
REPO = "acme/widgets"
WAITS = "This waits for the owner's review."
WAITING_BODY = "waiting on the owner"
REF = REPO + "#5"


class Hub:
    """Answers the three reads of one repo: its open list, its closed list and its recent comments."""
    def __init__(self, pr, comment):
        self.pr, self.comment = pr, comment

    def read_pages(self, path, cap=1):
        if "/comments" in path:
            return self.comments(), False
        return ([self.pr] if "state=open" in path else []), False

    def comments(self):
        return [{"issue_url": "https://api.github.com/repos/%s/issues/5" % REPO, "created_at": "2027-01-15T07:00:00Z", "id": 1, "body": self.comment}] if self.comment else []

    def _read_one(self, repo, path, now, fresh=False):
        return self.pr

    def read_comments(self, who, repo, since, cap=1, budget=None):
        return self.comments(), False


class TestSweepS2Automatic(Base):
    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("coder",))
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        self.engine = types.SimpleNamespace(state_dir=Path(self.world.state), now=lambda: NOW, config={}, hub=None,
                                            _note=lambda *a: None)

    def judge(self, labels, body, comment):
        """(the sweep's finding set, whether the confirming read still says S2) for one PR, through the real read and finding code."""
        cfg = dict(sb.SWEEP_DEFAULTS)
        regex = sb.sweep_phrase_re(cfg["person_names"])
        pr = {"number": 5, "pull_request": {"url": "u"}, "labels": [{"name": n} for n in labels], "body": body, "user": {"login": "cloud-pod"},
              "state": "open", "created_at": "2027-01-01T00:00:00Z", "updated_at": "2027-01-01T00:00:00Z"}
        hub = Hub(pr, comment)
        self.engine.hub = hub
        state = types.SimpleNamespace(repos={}, sources={}, unattributed=0, strong=sb.sweep_strong_re(cfg["person_names"]))
        state.repos[REPO] = sb.sweep_read_repo(hub, REPO, NOW, 7, regex, [50], state.strong)
        found = set(sb.sweep_findings(self.engine, state, [], REPO, NOW, cfg, regex))
        verify = sb.sweep_verify(self.engine, state, "S2", REF, NOW, cfg, regex, [50])
        return found, verify

    def assert_s2(self, labels, body, comment, want):
        found, verify = self.judge(labels, body, comment)
        self.assertEqual(("S2", REF) in found, want, "the finding")
        if want:
            self.assertTrue(verify, "the confirming read agrees")
        else:
            self.assertFalse(verify, "the confirming read agrees")

    def test_a_needs_captain_review_label_with_an_automatic_line_is_not_held_for_the_person(self):
        self.assert_s2(["needs:captain"], "MERGE: automatic\n\nPlan: n/a", WAITS, False)

    def test_an_automatic_line_after_a_blank_line_and_indented_still_counts(self):
        self.assert_s2(["needs:captain"], "\n  MERGE: automatic\n" + WAITING_BODY, None, False)

    def test_the_review_label_is_never_read_as_a_hold_for_a_pr_without_the_line(self):
        self.assert_s2(["needs:captain"], "A change.\n" + WAITING_BODY, None, True)

    def test_a_captain_line_with_waiting_text_and_no_label_is_s2_once_the_line_is_not_read(self):
        with mock.patch.object(sb, "CAPTAIN_MARKER_UNTIL", None):   # the real clock must not decide the test
            self.assert_s2([], "MERGE: captain\n\n" + WAITING_BODY, WAITS, True)

    def test_an_automatic_line_that_is_not_the_first_line_is_not_exempt(self):
        self.assert_s2(["needs:captain"], "Some text\nMERGE: automatic\n", WAITS, True)

    def test_no_markers_and_text_that_waits_is_s2(self):
        self.assert_s2([], "A change.\n" + WAITING_BODY, None, True)
        self.assert_s2([], "A change.", WAITS, True)

    def test_an_automatic_line_beside_an_owner_hold_label_is_not_exempt(self):
        for label in ("fleet:hold-security", "fleet:hold-owner", "x:hold-policy"):
            with self.subTest(label=label):
                self.assert_s2([label], "MERGE: automatic\n" + WAITING_BODY, None, True)

    def test_the_merge_captain_label_with_an_automatic_line_keeps_s6b_and_raises_no_s2(self):
        found, _ = self.judge(["fleet:merge-captain"], "MERGE: automatic\n" + WAITING_BODY, WAITS)
        self.assertNotIn(("S2", REF), found)
        self.assertIn(("S6b", REF), found)
