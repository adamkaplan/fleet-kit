"""#1886 Sixth amendment S11-S15: one alert path for every read of open items that stops at the ceiling, from the flag of the read itself."""
import re
import shutil
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

from support.core import Base
from support.loader import copy_entry, _load, sb
from test_open_list_ceiling import PagedHub, NOW

DAY = 86400.0


def full_api(path, repo=None, meta=None):
    return [{"number": 1, "state": "open", "labels": [], "title": "t", "body": ""}] * 100


class CutsBase(Base):
    def setUp(self):
        super().setUp()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.audit, self.reminders, self.notes = [], [], []
        self.cos = {"cos": types.SimpleNamespace(fleet={"role": "chief-of-staff"})}
        self.engine = types.SimpleNamespace(state_dir=str(self.state_dir), actor="daemon", discovery=types.SimpleNamespace(agents=self.cos),
                                            audit=types.SimpleNamespace(append=lambda *a: self.audit.append(a)))
        real_rem, real_note = sb.add_repeat_reminder, sb.note_open_cut
        sb.add_repeat_reminder = lambda *a, **k: self.reminders.append(a) or real_rem(*a, **k)
        sb.note_open_cut = lambda repo: self.notes.append(repo) or real_note(repo)
        self.addCleanup(setattr, sb, "add_repeat_reminder", real_rem)
        self.addCleanup(setattr, sb, "note_open_cut", real_note)
        sb.take_open_cuts()
        self.addCleanup(sb.take_open_cuts)

    def lines(self, kind):
        return [a for a in self.audit if a[1] == kind]

    def end_of_pass(self, now):
        for repo in sb.take_open_cuts():
            sb.open_list_alert(self.engine, repo, now)

    def five_reads(self, total=2000, repo="acme/api"):
        hub = PagedHub(total)
        sb.sweep_read_repo(hub, repo, NOW, 7, sb.sweep_phrase_re(["Alex"]), [100], sb.SweepState().strong)
        sb.triage_read_unit(hub, repo, "issues", NOW, [100])
        engine = types.SimpleNamespace(config={"plan_gates": {"repos": {repo: {}}}}, state_dir=str(self.state_dir), audit=self.engine.audit,
                                       hub=types.SimpleNamespace(read_pages=lambda path, key=None, timeout=None, older=None, cap=None: hub.read_pages(path, cap=cap)),
                                       now=lambda: NOW)
        sb.ci_record_pass(engine, NOW)
        sb.open_pages(lambda page: full_api("labels"), repo)
        reader = sb.IntentReader.__new__(sb.IntentReader)
        reader.repo, reader._cache, reader.now, reader._api = repo, {}, lambda: NOW, full_api
        reader.open_asks(5)


class TestOpenListCuts(CutsBase):
    def test_s11_five_reads_note_five_and_the_pass_raises_one_alert_and_the_next_day_one_more(self):
        self.five_reads()
        self.assertEqual(self.notes, ["acme/api"] * 5, msg="each of the five readers notes the repository once")
        self.end_of_pass(NOW)
        self.assertEqual((len(self.lines("open_list.cut")), len(self.reminders)), (1, 1))
        self.assertEqual(sb.take_open_cuts(), [])
        self.five_reads()
        self.end_of_pass(NOW + DAY)
        self.assertEqual((len(self.lines("open_list.cut")), len(self.reminders)), (2, 2))

    def test_s11_control_an_uncut_list_notes_nothing(self):
        self.five_reads(total=300)
        self.assertEqual(self.notes, ["acme/api"] * 2, msg="a list of 300 is read whole: only the two fake full-page readers (labels and asks) note")

    def test_s12_a_repository_that_dropped_below_the_ceiling_raises_nothing(self):
        self.five_reads()
        self.end_of_pass(NOW)
        self.assertEqual(len(self.reminders), 1)
        sb.sweep_read_repo(PagedHub(300), "acme/api", NOW + 900, 7, sb.sweep_phrase_re(["Alex"]), [100], sb.SweepState().strong)
        self.assertEqual(sb.take_open_cuts(), [], msg="the set is empty after the second pass")
        self.end_of_pass(NOW + 900)
        self.end_of_pass(NOW + DAY)
        self.assertEqual(len(self.reminders), 1, msg="no alert from an older read")

    def test_s13_no_chief_of_staff_marks_nothing_and_the_next_read_alerts(self):
        self.cos.clear()
        sb.note_open_cut("acme/api")
        self.end_of_pass(NOW)
        self.assertEqual((len(self.reminders), len(self.lines("open_list.cut_unsent"))), (0, 1))
        self.assertFalse((self.state_dir / "open-list-alert.json").exists(), msg="no day marker without a delivered reminder")
        self.cos["cos"] = types.SimpleNamespace(fleet={"role": "chief-of-staff"})
        sb.note_open_cut("acme/api")
        self.end_of_pass(NOW + 900)
        self.assertEqual(len(self.reminders), 1)
        self.assertTrue((self.state_dir / "open-list-alert.json").exists())

    def test_s13_a_failed_marker_write_is_one_audit_line(self):
        sb.note_open_cut("acme/api")
        alerts = sb.load_module("open_list_alert")
        with mock.patch.object(alerts, "mark", lambda *a: False):
            self.end_of_pass(NOW)
        self.assertEqual((len(self.reminders), len(self.lines("open_list.marker_failed"))), (1, 1))


class TestOpenListPagesRead(CutsBase):
    def test_s14_the_boundaries_and_a_failed_later_page(self):
        for total, cut in ((1000, True), (999, False), (0, False), (1001, True)):
            sb.take_open_cuts()
            seen = []

            def read(page, total=total):
                seen.append(page)
                return [{}] * max(0, min(100, total - (page - 1) * 100))
            got = sb.open_pages(read, "acme/api")
            self.assertEqual(sb.take_open_cuts() == ["acme/api"], cut, msg="total %d" % total)
            self.assertEqual(len(got), min(total, 1000))
            if total == 1001:
                self.assertEqual(len(seen), 10)

        def failing(page):
            return [{}] * 100 if page < 3 else "boom"
        with self.assertRaises(sb.IntentError):
            sb.open_pages(failing, "acme/api")
        self.assertEqual(sb.open_pages(lambda page: "boom", "acme/api"), "boom")

    def test_s14_the_hub_reads_ten_pages_for_1001_items(self):
        hub = PagedHub(1001)
        records, cut = hub.read_open_pages("repos/acme/api/issues?state=open&per_page=100")
        self.assertEqual((len(records), cut, len(hub.open_requests())), (1000, True, 10))


class TestOpenListDerivedText(Base):
    """S15: the text and LABEL_HOLD_LIST_CUT follow OPEN_LIST_PAGES in a copy of the entry (control first)."""

    def load(self, edit, tag):
        tmp = tempfile.mkdtemp()   # kept until the test ends: the entry loads its modules lazily
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(str(Path(__file__).resolve().parents[2] / "bin"), tmp, dirs_exist_ok=True)   # the entry and the modules it loads by name
        copy_entry(tmp)
        entry = Path(tmp) / "fleet-switchboard"
        edited = edit(entry.read_text())
        entry.write_text(edited)
        return edited, _load("t_open_cuts_" + tag, entry)

    def alert_text(self, mod, state):
        sent = []
        engine = types.SimpleNamespace(state_dir=state, actor="d", audit=None, discovery=types.SimpleNamespace(
            agents={"c": types.SimpleNamespace(fleet={"role": "chief-of-staff"})}))
        mod.add_repeat_reminder = lambda *a, **k: sent.append(a)
        mod.open_list_alert(engine, "acme/api", NOW)
        return sent[0][4]

    def test_s15_the_text_and_the_hold_cut_follow_the_ceiling(self):
        line = "OPEN_LIST_PAGES = 10 "
        _, control = self.load(lambda t: t, "control")
        self.assertEqual(control.LABEL_HOLD_LIST_CUT, 1000)
        self.assertEqual(sb.LABEL_HOLD_LIST_CUT, 1000)
        self.assertIn("1,000 or more", self.alert_text(control, str(self.make_dir("a"))))
        applied = []

        def mutate(text):
            self.assertEqual(text.count(line), 1)
            applied.append(True)
            return text.replace(line, "OPEN_LIST_PAGES = 5  ", 1)
        edited, copy = self.load(mutate, "mutant")
        self.assertTrue(applied)
        self.assertIn("OPEN_LIST_PAGES = 5 ", edited)
        self.assertEqual(copy.LABEL_HOLD_LIST_CUT, 500, msg="the hold cut follows the ceiling")
        self.assertIn("500 or more", self.alert_text(copy, str(self.make_dir("b"))), msg="the alert text names the ceiling of the constants")

    def make_dir(self, name):
        path = Path(self.state_dir) / name
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path


if __name__ == "__main__":
    unittest.main()
