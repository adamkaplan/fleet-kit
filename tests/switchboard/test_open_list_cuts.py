"""#1886 Sixth amendment S11-S15: one alert path for every read of open items that stops at the ceiling, from the flag of the read itself."""
import ast
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

from support.core import Base
from support.paths import BIN
from support.loader import copy_entry, _load, sb
from test_open_list_ceiling import PagedHub, NOW, edit_program

DAY = 86400.0


def full_api(path, repo=None, meta=None):
    return [{"number": 1, "state": "open", "labels": [], "title": "t", "body": ""}] * 100


def real_hub(total, requests=None):
    """The REAL GitHubHub paging (read_pages, read_open_pages) over a fake gh_api: a clamp at 5 pages shows here."""
    hub = sb.GitHubHub.__new__(sb.GitHubHub)
    items = [{"number": n, "state": "open", "title": "t", "body": "", "labels": [], "user": {"login": "u"}, "created_at": "2026-10-01T00:00:00Z",
              "updated_at": "2026-10-01T00:00:00Z", "comments": 0} for n in range(total, 0, -1)]

    def gh_api(path, paginate=True, timeout=None):
        if requests is not None:
            requests.append(path)
        if "state=open" not in path:
            return []
        number = int(re.search(r"[?&]page=(\d+)", path).group(1))
        return [items[(number - 1) * 100:number * 100]]
    hub.gh_api = gh_api
    hub.read_comments = lambda consumer, repo, since, cap: hub.read_pages(   # #1889: what read_pages answered for the comments path
        "repos/%s/issues/comments?since=%s&sort=created&direction=desc&per_page=100" % (repo, since), older=("created_at", since), cap=cap)
    return hub


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
        self.requests = []
        hub = real_hub(total, self.requests)
        sb.sweep_read_repo(hub, repo, NOW, 7, sb.sweep_phrase_re(["Alex"]), [100], sb.SweepState().strong)
        sb.triage_read_unit(hub, repo, "issues", NOW, [100])
        engine = types.SimpleNamespace(actor="daemon", config={"plan_gates": {"repos": {repo: {}}}}, actor="daemon", state_dir=str(self.state_dir), audit=self.engine.audit,
                                       hub=hub,
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
        self.assertEqual(len([r for r in self.requests if "state=open" in r]), 30, msg="ten pages each for the sweep, the triage and the recorder (a clamp at 5 pages reads 15)")
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
        sb.sweep_read_repo(real_hub(300), "acme/api", NOW + 900, 7, sb.sweep_phrase_re(["Alex"]), [100], sb.SweepState().strong)
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

    def test_two_spellings_of_one_repository_are_one_note_and_one_alert(self):
        sb.note_open_cut("Acme/API")
        sb.note_open_cut("acme/api")
        self.assertEqual(sb.take_open_cuts(), ["acme/api"])
        sb.note_open_cut("Acme/API")
        sb.note_open_cut("acme/api")
        self.end_of_pass(NOW)
        self.assertEqual((len(self.reminders), len(self.lines("open_list.cut"))), (1, 1))

    def test_cut_unsent_is_one_line_a_day_per_repository_and_then_a_chief_of_staff_alerts(self):
        sb._UNSENT_DAY.clear()
        self.addCleanup(sb._UNSENT_DAY.clear)
        saved = dict(self.cos)
        self.cos.clear()
        for n in range(3):
            sb.note_open_cut("acme/api")
            self.end_of_pass(NOW + n * 900)
        self.assertEqual(len(self.lines("open_list.cut_unsent")), 1, msg="three passes without a chief of staff write one line")
        self.cos.update(saved)
        sb.note_open_cut("acme/api")
        self.end_of_pass(NOW + 3 * 900)
        self.assertEqual((len(self.reminders), len(self.lines("open_list.cut"))), (1, 1))

    def test_s13_a_failed_marker_write_is_one_audit_line(self):
        sb.note_open_cut("acme/api")
        alerts = sb.load_module("open_list_alert")
        with mock.patch.object(alerts, "mark", lambda *a: False):
            self.end_of_pass(NOW)
        self.assertEqual((len(self.reminders), len(self.lines("open_list.marker_failed"))), (1, 1))

    def test_a_cut_noted_by_the_recorder_is_raised_in_the_same_pass_of_the_real_publish_decisions(self):
        """The REAL publish_decisions and the REAL ci_record_pass over the real paging (a fake gh_api); the passes before it (derive, sweep, triage) and after it are replaced by no-ops (the other passes are not under test: each is a fake)."""
        hub = real_hub(2000)
        engine = types.SimpleNamespace(actor="daemon", config={"plan_gates": {"repos": {"acme/api": {}}}, "github_repos": ["acme/api"]}, state_dir=str(self.state_dir), actor="daemon",
                                       discovery=types.SimpleNamespace(agents=self.cos), audit=types.SimpleNamespace(append=lambda *a: self.audit.append(a)),
                                       hub=hub, now=lambda: NOW, _note=lambda *a, **k: None)
        quiet = lambda *a, **k: None
        with mock.patch.object(sb, "derive_decisions", lambda e, n: ([], [])), mock.patch.object(sb, "sweep_decisions", lambda e, d, n, errs: d), \
                mock.patch.object(sb, "triage_pass", quiet), mock.patch.object(sb, "pr_events_pass", quiet), mock.patch.object(sb, "pane_health_pass", quiet), \
                mock.patch.object(sb, "decision_store", lambda e: types.SimpleNamespace(publish=lambda *a, **k: True)), \
                mock.patch.object(sb, "chat_clear_pass", quiet), mock.patch.object(sb, "apply_notice_effects", quiet), mock.patch.object(sb, "prune_escalations_pass", quiet), \
                mock.patch.object(sb, "sync_external", lambda *a, **k: []), mock.patch.object(sb, "work_sync_external", quiet):
            sb.publish_decisions(engine)
        self.assertEqual(len(self.lines("open_list.cut")), 1, msg="the recorder's cut is raised in the pass that read it")
        self.assertEqual(sb.take_open_cuts(), [])


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
        requests = []
        hub = real_hub(1001, requests)
        records, cut = hub.read_open_pages("repos/acme/api/issues?state=open&per_page=100")
        self.assertEqual((len(records), cut, len(requests)), (1000, True, 10), msg="a clamp at 5 pages fails here")
        self.assertEqual(sb.take_open_cuts(), ["acme/api"])


class TestOpenListDerivedText(Base):
    """S15: the text follows OPEN_LIST_PAGES in a copy of the entry (control first)."""

    def load(self, edit, tag):
        tmp = tempfile.mkdtemp()   # kept until the test ends: the entry loads its modules lazily
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(str(Path(__file__).resolve().parents[2] / "bin"), tmp, dirs_exist_ok=True)   # the entry and the modules it loads by name
        copy_entry(tmp)
        entry = Path(tmp) / "fleet-switchboard"
        edited = edit_program(tmp, edit)
        return edited, _load("t_open_cuts_" + tag, entry)

    def alert_text(self, mod, state):
        sent = []
        engine = types.SimpleNamespace(state_dir=state, actor="d", audit=None, discovery=types.SimpleNamespace(
            agents={"c": types.SimpleNamespace(fleet={"role": "chief-of-staff"})}))
        mod.add_repeat_reminder = lambda *a, **k: sent.append(a)
        mod.open_list_alert(engine, "acme/api", NOW)
        return sent[0][4]

    def test_s15_the_text_follows_the_ceiling_and_no_second_copy_exists(self):
        line = "OPEN_LIST_PAGES = 10 "
        _, control = self.load(lambda t: t, "control")
        self.assertEqual(self.definitions(), 0, msg="LABEL_HOLD_LIST_CUT is defined nowhere over bin/")
        self.assertIn("1,000 or more", self.alert_text(control, str(self.make_dir("a"))))
        applied = []

        def mutate(text):
            self.assertEqual(text.count(line), 1)
            applied.append(True)
            return text.replace(line, "OPEN_LIST_PAGES = 5  ", 1)
        edited, copy = self.load(mutate, "mutant")
        self.assertTrue(applied)
        self.assertIn("OPEN_LIST_PAGES = 5 ", edited)
        self.assertIn("500 or more", self.alert_text(copy, str(self.make_dir("b"))), msg="the alert text names the ceiling of the constants")

    def definitions(self):
        done = subprocess.run(["grep", "-rEc", "--exclude=test-*", "--exclude-dir=__pycache__", r"^LABEL_HOLD_LIST_CUT[[:space:]]*=", str(BIN)], capture_output=True, text=True)
        return sum(int(line.rpartition(":")[2]) for line in done.stdout.splitlines())

    def make_dir(self, name):
        path = Path(self.state_dir) / name
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path


def drain_two_with_a_failing_first(mod, notes_out):
    """The REAL publish_decisions of `mod` with two repositories noted and open_list_alert raising for a/a only; returns the alert counts."""
    mod.take_open_cuts()
    mod.note_open_cut("a/a")
    mod.note_open_cut("b/b")
    seen = []

    def alert(engine, repo, now):
        seen.append(repo)
        if repo == "a/a":
            raise RuntimeError("boom")
    engine = types.SimpleNamespace(actor="daemon", config={"plan_gates": {"repos": {}}, "github_repos": []}, state_dir="/nonexistent", actor="daemon",
                                   discovery=types.SimpleNamespace(agents={}), audit=types.SimpleNamespace(append=lambda *a: None),
                                   hub=None, now=lambda: NOW, _note=lambda *a, **k: notes_out.append(a))
    quiet = lambda *a, **k: None
    with mock.patch.object(mod, "open_list_alert", alert), mock.patch.object(mod, "derive_decisions", lambda e, n: ([], [])), \
            mock.patch.object(mod, "sweep_decisions", lambda e, d, n, errs: d), mock.patch.object(mod, "triage_pass", quiet), \
            mock.patch.object(mod, "pr_events_pass", quiet), mock.patch.object(mod, "ci_record_pass", quiet), mock.patch.object(mod, "pane_health_pass", quiet), \
            mock.patch.object(mod, "decision_store", lambda e: types.SimpleNamespace(publish=lambda *a, **k: True)), \
            mock.patch.object(mod, "chat_clear_pass", quiet), mock.patch.object(mod, "apply_notice_effects", quiet), mock.patch.object(mod, "prune_escalations_pass", quiet), \
            mock.patch.object(mod, "sync_external", lambda *a, **k: []), mock.patch.object(mod, "work_sync_external", quiet):
        mod.publish_decisions(engine)
    return seen


class TestOpenListCutsMutants(Base):
    """S11 and S12 mutants: one textual replacement each in a copy of the entry, control first, the mutation asserted applied."""

    def load(self, edit, tag):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(str(Path(__file__).resolve().parents[2] / "bin"), tmp, dirs_exist_ok=True)
        copy_entry(tmp)
        entry = Path(tmp) / "fleet-switchboard"
        edited = edit_program(tmp, edit)
        return edited, _load("t_open_cuts_m_" + tag, entry)

    def reads(self, mod, total):
        hub = mod.GitHubHub.__new__(mod.GitHubHub)
        hub.gh_api = lambda path, paginate=True, timeout=None: ([[{"number": n, "state": "open", "labels": [], "title": "t", "body": ""}
                                                                  for n in range(total - (int(re.search(r"[?&]page=(\d+)", path).group(1)) - 1) * 100, 0, -1)][:100]]
                                                                if "state=open" in path else [])
        hub.read_open_pages("repos/acme/api/issues?state=open&per_page=100")
        mod.triage_read_unit(hub, "acme/api", "issues", NOW, [100])

    def run_pair(self, needle, replacement, tag, check):
        _, control = self.load(lambda t: t, tag + "_control")
        check(control)
        applied = []

        def mutate(text):
            self.assertEqual(text.count(needle), 1)
            applied.append(True)
            return text.replace(needle, replacement, 1)
        edited, mutant = self.load(mutate, tag)
        self.assertTrue(applied)
        self.assertIn(replacement, edited)
        with self.assertRaises(AssertionError) as caught:
            check(mutant)
        return str(caught.exception)

    def test_s11_a_build_that_alerts_at_each_read_fails_with_one_alert_for_five_reads(self):
        def check(mod):
            mod.take_open_cuts()
            self.reads(mod, 2000)
            self.assertEqual(mod.take_open_cuts(), ["acme/api"], msg="one alert for five reads: the reads note, the end of the pass alerts")
        text = self.run_pair("    _OPEN_CUTS.add(str(repo).lower())   # one spelling per repository\n", "    try:\n        open_list_alert(None, repo, 0.0)   # the mutant: an alert at each read\n    except Exception:\n        pass\n", "s11", check)
        self.assertIn("one alert for five reads", text)

    def test_s12_a_set_never_emptied_fails_with_no_stale_cut(self):
        def check(mod):
            mod.take_open_cuts()
            self.reads(mod, 2000)
            self.assertEqual(mod.take_open_cuts(), ["acme/api"])
            self.reads(mod, 300)
            self.assertEqual(mod.take_open_cuts(), [], msg="no stale cut")
        text = self.run_pair("    _OPEN_CUTS.clear()\n", "    pass\n", "s12", check)
        self.assertIn("no stale cut", text)

    def test_one_failing_alert_loses_the_other_cuts_of_the_pass(self):
        def check(mod):
            notes = []
            seen = drain_two_with_a_failing_first(mod, notes)
            self.assertEqual(seen.count("b/b"), 1, msg="one failing alert loses the other cuts of the pass")
            self.assertEqual([n for n in notes if n[0] == "open_list.error"].__len__(), 1)
        loop = '    for repo in take_open_cuts():   # #1886: the daily alert for every read of this pass that stopped at the ceiling\n        try:\n            open_list_alert(engine, repo, now)\n        except Exception as err:\n            engine._note("open_list.error", repo, {"error": _one_line("%s: %s" % (type(err).__name__, err), 200), "repo": repo})\n'
        mutated = '    try:\n        for repo in take_open_cuts():\n            open_list_alert(engine, repo, now)\n    except Exception as err:\n        engine._note("open_list.error", None, {"error": _one_line("%s: %s" % (type(err).__name__, err), 200)})\n'
        text = self.run_pair(loop, mutated, "drain", check)
        self.assertIn("one failing alert loses the other cuts of the pass", text)

    def held_rows_after_a_failed_read(self, mod):
        """The REAL ci_record_pass and the REAL label_hold_entries: a cut list (1,200 records) whose held pull request fell beyond the first 1,000, then a failed read."""
        def listing(held_in):
            top = 1300 if held_in else 2500   # newer pull requests push the held one beyond the first 1,000
            items = [{"number": n, "state": "open", "title": "t", "body": "", "labels": [], "head": {"sha": "a" * 40}, "created_at": "2026-10-01T00:00:00Z"}
                     for n in range(top, top - 1200, -1)]   # 1,200 records, newest first: the ceiling reads the newest 1,000
            if held_in:
                items[0]["labels"] = [{"name": "tester:merge-captain"}]   # the held pull request is read while it is among the newest
            return items
        mode = ["in"]

        def gh_api(path, paginate=True, timeout=None):
            if mode[0] == "fail":
                raise mod.GitHubUnavailable("down")
            items = listing(mode[0] == "in")
            number = int(re.search(r"[?&]page=(\d+)", path).group(1))
            return [items[(number - 1) * 100:number * 100]]
        hub = mod.GitHubHub.__new__(mod.GitHubHub)
        hub.gh_api = gh_api
        engine = types.SimpleNamespace(actor="daemon", config={"plan_gates": {"repos": {"acme/api": {}}}, "github_repos": ["acme/api"]}, actor="daemon", state_dir=str(self.state_dir), audit=types.SimpleNamespace(append=lambda *a: None),
                                       discovery=None, hub=hub, now=lambda: NOW, _note=lambda *a, **k: None, request_keys=set())
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        mod.ci_record_pass(engine, NOW)
        self.assertTrue(engine.ci_record_state.was_cut.get("acme/api"), msg="the 1,200 list was cut at the ceiling")
        self.assertTrue(mod.label_hold_entries(engine, NOW, set(), set(), ["acme/api"]), msg="the held row is shown from the first read")
        mode[0] = "out"   # the held pull request is no longer in the newest 1,000 (a cut list)
        mod.ci_record_pass(engine, NOW + 1000)
        self.assertTrue(mod.label_hold_entries(engine, NOW + 1000, set(), set(), ["acme/api"]), msg="a cut list keeps the row")
        mode[0] = "fail"
        mod.ci_record_pass(engine, NOW + 2000)
        rows = mod.label_hold_entries(engine, NOW + 2000, set(), set(), ["acme/api"])
        self.assertTrue(rows, msg="a failed read of a cut list keeps the held rows")

    def test_was_cut_a_failed_read_of_a_cut_list_keeps_the_held_rows(self):
        needle = "state.good.pop(key, None)   # a failed read is no evidence"
        _, control = self.load(lambda t: t, "wc_control")
        self.held_rows_after_a_failed_read(control)
        text = None
        applied = []

        def mutate(t):
            self.assertEqual(t.count(needle), 1)
            applied.append(True)
            return t.replace(needle, "state.was_cut.pop(key, None); " + needle, 1)
        _, mutant = self.load(mutate, "wc")
        self.assertTrue(applied)
        with self.assertRaises(AssertionError) as caught:
            self.held_rows_after_a_failed_read(mutant)
        self.assertIn("a failed read of a cut list keeps the held rows", str(caught.exception))


class TestOpenListWholeReads(CutsBase):
    """S7: the REAL read_charters (a closure of load_tick, taken from the context load_tick builds) and the REAL ci_record_pass; only the load alert's health module is a stand-in that hands the context back."""

    def charters(self):
        labels = [{"name": "tester:orchestrator"}] + [{"name": "l%d" % n} for n in range(119)]
        charters = [{"number": n, "body": "b"} for n in range(1000, 890, -1)]   # 110 charters, one page of 100 and a second of 10
        calls = []

        def api(path, repo=None, meta=None):
            calls.append(path)
            page = int((re.search(r"[?&]page=(\d+)", path) or [0, 1])[1] if re.search(r"[?&]page=(\d+)", path) else 1)
            if "/labels" in path:
                return labels[(page - 1) * 100:page * 100]
            return charters[(page - 1) * 100:page * 100]
        captured = {}
        health = types.SimpleNamespace(DEFAULTS={}, Context=lambda **kw: captured.update(kw) or types.SimpleNamespace(), tick=lambda c: [])
        engine = types.SimpleNamespace(config={}, state_dir=str(self.state_dir), actor="daemon", audit=None, now=lambda: NOW, awaiting_first_full_pass=False,
                                       intents=types.SimpleNamespace(_api=api, repo="acme/api"), discovery=types.SimpleNamespace(agents={}))
        with mock.patch.object(sb, "load_fleet_health", lambda: health):
            sb.load_tick(engine, engine.discovery)
        return captured["read_charters"]("acme/api"), calls

    def test_s7_the_real_read_charters_reads_110_charters_and_the_recorder_250_pull_requests(self):
        got, calls = self.charters()
        self.assertEqual(len(got), 110, msg="110 charters read over two pages")
        self.assertEqual(len([c for c in calls if "/labels" in c]), 2, msg="120 labels are two pages")
        hub = real_hub(0)
        prs = [{"number": n, "state": "open", "title": "t", "body": "", "labels": [], "head": {"sha": "a" * 40}} for n in range(250, 0, -1)]
        hub.gh_api = lambda path, paginate=True, timeout=None: [prs[(int(re.search(r"[?&]page=(\d+)", path).group(1)) - 1) * 100:int(re.search(r"[?&]page=(\d+)", path).group(1)) * 100]]
        engine = types.SimpleNamespace(actor="daemon", config={"plan_gates": {"repos": {"acme/api": {}}}}, actor="daemon", state_dir=str(self.state_dir), audit=self.engine.audit, hub=hub, now=lambda: NOW,
                                       _note=lambda *a, **k: None)
        sb.ci_record_pass(engine, NOW)
        self.assertEqual(len(engine.ci_record_state.lists["acme/api"][1]), 250, msg="all 250 pull requests are read (a cap of 2 pages reads 200)")


class TestOpenListCutsWired(Base):
    def test_the_classes_on_the_loader_line_are_the_test_classes_of_the_file(self):
        root = Path(__file__).resolve().parents[2]
        line = next(l for l in (root / "bin" / "test-switchboard").read_text().splitlines() if l.startswith('_olcuts = importlib.import_module("test_open_list_cuts")'))
        named = set(re.findall(r"\bTest\w+", line.split(";", 1)[1].split("=")[0]))
        tree = ast.parse((Path(__file__)).read_text())
        defined = {n.name for n in tree.body if isinstance(n, ast.ClassDef) and n.name.startswith("Test")}
        self.assertEqual(named, defined, msg="the classes named on the loader line equal the test classes of the file")


if __name__ == "__main__":
    unittest.main()
