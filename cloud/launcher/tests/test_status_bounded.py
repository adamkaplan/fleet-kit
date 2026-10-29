#!/usr/bin/env python3
"""The cloud rows of `status` stay bounded and complete (ask #1493): the launcher's real list answer is read, ONE read serves every worker, the
probes run four at a time, `--lane` narrows the rows, and a worker whose read fails shows its own reason.

Offline: the fake kubectl of the status summary tests is the cluster; the launcher's status answer and the reachability probe are fakes.
"""
import contextlib
import json
import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_cloud_status_summary import ALIVE, CLOUD, KIT, Base, Path   # noqa: E402  (the same loader and fakes)


def real_shape(name, phase="Running"):
    """What `cloud-worker status NAME --json` really prints for one worker: an object inside a list."""
    return {"cost_estimate_usd": 0.42, "deadlines": {"hard": ALIVE, "idle": "2026-10-28T12:00:00Z"}, "idle_since": None, "phase": phase,
            "runtime_id": "pod/cw-" + name, "worker": name}


class Fakes:
    """The fakes the status tests share (a mixin: it holds helpers only, no test method)."""
    DELAY = 0.0

    def setUp(self):
        super().setUp()
        self.reads, self.probes = [], []
        self.lock = threading.Lock()
        self.running, self.peak = 0, 0   # the probes in flight now, and the most seen at once

    def live(self, count, lane_of=lambda i: "orch"):
        names = ["w%d" % i for i in range(count)]
        for i, name in enumerate(names):
            self.record(name, hard=ALIVE, reports_to=lane_of(i))
        return names

    def fakes(self, answer, down=(), drop_lists=False, delay=None):
        """Patch the launcher's status read (the other launcher commands go on to the real run) and the probe."""
        delay = self.DELAY if delay is None else delay
        module = self.cloud()
        real = module.run_launcher

        def launcher(runner, command, worker, *args, **kw):
            if command != "status":
                return real(runner, command, worker, *args, **kw)
            with self.lock:
                self.reads.append(worker)
            time.sleep(delay)
            if drop_lists:
                self.applied = True   # the mutation ran: the list answer was turned into "nothing read"
                return 0, None, ""
            asked = [worker] if worker else None
            body = [answer(n) for n in (asked or self.names) if n not in down]
            return 0, body, ""

        def probe(state, cluster, record, timeout):
            with self.lock:
                self.probes.append(record["worker"])
                self.running += 1
                self.peak = max(self.peak, self.running)
            time.sleep(delay)
            with self.lock:
                self.running -= 1
            return True

        stack = [mock.patch.object(module, "run_launcher", launcher), mock.patch.object(module, "_answers", probe)]
        for patch in stack:
            patch.start()
            self.addCleanup(patch.stop)


class Bounded(Fakes, Base):
    def check_real_shape(self):
        """The S2 claims, a function so the mutant can run it."""
        self.names = self.live(1)
        self.fakes(real_shape)
        lines, rows = self.status()
        line = [l for l in lines if "cloud w0" in l][0]
        self.assertNotIn("unavailable", line)
        self.assertIn("phase Running", line)
        self.assertIn("2026-10-28T12:00:00Z", line)
        self.assertIn("$0.42", line)
        return line

    def test_s2_the_launchers_real_list_shape_shows_phase_idle_deadline_and_cost(self):
        line = self.check_real_shape()
        self.assertIn("idle deadline 2026-10-28T12:00:00Z", line)
        self.assertIn("answers", line)

    def test_s2_the_older_object_shape_still_works(self):
        self.names = self.live(1)
        self.fakes(real_shape)
        module = self.cloud()
        real = module.run_launcher
        stub = lambda runner, command, worker, *a, **k: (0, real_shape("w0"), "") if command == "status" else real(runner, command, worker, *a, **k)
        with mock.patch.object(module, "run_launcher", stub):
            lines, _ = self.status()
        self.assertIn("phase Running", [l for l in lines if "cloud w0" in l][0])

    def test_s3_fifteen_live_workers_finish_within_thirty_seconds_with_fifteen_lines(self):
        self.names = self.live(15)
        self.fakes(real_shape, delay=0.3)
        lines, rows = self.status()
        print("s3 most probes at once: %d" % self.peak, file=sys.stderr)
        self.assertGreater(self.peak, 1, "the probes ran one after another")
        self.assertLessEqual(self.peak, 4, "more than four probes ran at once")
        self.assertEqual(len([l for l in lines if l.strip().startswith("cloud w")]), 15, lines)
        self.assertEqual([r["worker"] for r in rows], sorted(self.names))   # the records' order
        self.assertEqual(len(self.reads), 1, self.reads)   # ONE read, without a worker name
        self.assertEqual(self.reads, [""])
        self.assertEqual(len(self.probes), 15)

    def test_s3_a_probe_that_raises_or_hangs_is_does_not_answer_for_that_worker_alone(self):
        self.names = self.live(3)
        self.fakes(real_shape)
        module = self.cloud()

        def probe(state, cluster, record, timeout):
            if record["worker"] == "w0":
                raise RuntimeError("tunnel failed")
            if record["worker"] == "w1":
                time.sleep(30)
            return True
        with mock.patch.object(module, "PROBE_SECONDS", 0.5), mock.patch.object(module, "_answers", probe):
            started = time.monotonic()
            lines, _ = self.status()
        # a hang guard only: the expected time is 0.5 s (the probe bound), the probe sleeps 30 s; 10 s is a factor 20 of the expected time
        self.assertLess(time.monotonic() - started, 10)
        by = {n: [l for l in lines if "cloud %s:" % n in l][0] for n in self.names}
        self.assertIn("does not answer", by["w0"])
        self.assertIn("does not answer", by["w1"])
        self.assertIn("; answers;", by["w2"])

    def test_s4_lane_keeps_the_workers_of_that_boss(self):
        self.names = self.live(6, lane_of=lambda i: "lane-a" if i % 2 else "lane-b")
        self.fakes(real_shape)
        module = self.cloud()
        import subprocess
        with mock.patch.object(module, "_answers", lambda *a: True):
            lines, rows = module.status_extra(subprocess.run, "lane-a")
        self.assertEqual(sorted(r["worker"] for r in rows), ["w1", "w3", "w5"])
        self.assertEqual(len([l for l in lines if l.strip().startswith("cloud w")]), 3, lines)
        code, out, err = self.run_command("status", "--lane", "lane-b")
        self.assertEqual(len([l for l in out.splitlines() if l.strip().startswith("cloud w")]), 3, out)

    def test_s4_without_the_flag_every_worker_shows(self):
        self.names = self.live(6, lane_of=lambda i: "lane-a" if i % 2 else "lane-b")
        self.fakes(real_shape)
        lines, rows = self.status()
        self.assertEqual(len(rows), 6)

    def test_s5_one_failing_read_shows_its_own_reason_and_the_rest_stay_complete(self):
        self.names = self.live(5)
        self.fakes(real_shape, down=("w2",))
        lines, _ = self.status()
        rows = [l for l in lines if l.strip().startswith("cloud w")]
        self.assertEqual(len(rows), 5, lines)
        bad = [l for l in rows if "unavailable" in l]
        self.assertEqual(len(bad), 1, lines)
        self.assertIn("cloud w2", bad[0])
        self.assertIn("not in the launcher's status", bad[0])
        self.assertEqual(len([l for l in rows if "phase Running" in l and "$0.42" in l]), 4, lines)

    def test_s5_a_failed_whole_read_names_its_reason_on_every_line(self):
        self.names = self.live(2)
        self.fakes(real_shape)
        module = self.cloud()
        real = module.run_launcher
        with mock.patch.object(module, "run_launcher", lambda r, c, w, *a, **k: (3, None, "boom") if c == "status" else real(r, c, w, *a, **k)):
            lines, _ = self.status()
        rows = [l for l in lines if l.strip().startswith("cloud w")]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("unavailable" in l for l in rows), rows)

    # ---- S9 the probe stage has one deadline

    def check_stage_deadline(self, count=300, stage=2, probe=1, mutate=False):
        """`count` live workers whose probes all hang: the whole status ends within the stage deadline + 2 s, some lines say `not probed`, nothing raises.
        `mutate`: the stage deadline is switched off (each probe bounded only) and the probes are short so the mutant stays quick."""
        self.names = self.live(count)
        self.fakes(real_shape)
        module = self.cloud()
        begun = []

        def hang(state, cluster, record, timeout):
            with self.lock:
                begun.append(record["worker"])
            time.sleep(30)
        stage_now, probe_now = (10 ** 6, 0.1) if mutate else (stage, probe)
        with mock.patch.object(module, "PROBE_STAGE_SECONDS", stage_now), mock.patch.object(module, "PROBE_SECONDS", probe_now), \
                mock.patch.object(module, "_answers", hang):
            started = time.monotonic()
            lines, rows = self.status()
            took = time.monotonic() - started
        print("stage %s took %.1f s" % ("mutant" if mutate else "real", took), file=sys.stderr)
        self.assertEqual(len(rows), count)
        mine = [l for l in lines if l.strip().startswith("cloud w")]
        self.assertEqual(len(mine), count)
        not_probed = len([l for l in mine if "not probed" in l])
        print("stage: %d probes begun, %d lines not probed" % (len(begun), not_probed), file=sys.stderr)
        self.assertTrue(not_probed, "no line says not probed")
        self.assertLess(len(begun), count, "every probe was begun: the stage deadline did not stop the starts")
        # a hang guard only: the expected time is the stage deadline (2 s); 40 s is a factor 20 of it
        self.assertLess(took, stage * 20, took)
        return mine

    def test_s9_three_hundred_hanging_workers_end_at_the_stage_deadline_with_not_probed_lines(self):
        mine = self.check_stage_deadline()
        self.assertTrue(any("does not answer" in l for l in mine))   # a probe running at the deadline is given up
        self.assertEqual([l.split()[1].rstrip(":") for l in mine], sorted(self.names))   # the records' order

    def test_s9_a_mutant_without_the_stage_deadline_fails_the_three_hundred_test(self):
        with self.assertRaises(AssertionError):
            self.check_stage_deadline(mutate=True)

    def test_s9_the_stage_deadline_is_a_named_thirty_second_constant(self):
        module = self.cloud()
        self.assertEqual(module.PROBE_STAGE_SECONDS, 30)
        self.assertEqual(module.SNAPSHOT_SECONDS, 90)

    # ---- S10 the launcher read is bounded by the number of records

    def check_read_timeouts(self, count=60):
        """`count` live records, the launcher read takes 0.05 s a record (scaled): the derived timeout holds it, a fixed one does not."""
        self.names = self.live(count)
        self.fakes(real_shape)
        module = self.cloud()
        seen = []

        def launcher(runner, command, worker, options=(), timeout=0, *a, **k):
            if command != "status":
                return real(runner, command, worker, options, timeout, *a, **k)
            need = 0.05 * count
            seen.append(timeout)
            if timeout < need:
                raise module.CloudError("the launcher did not finish status within %s s" % timeout)
            time.sleep(need)
            return 0, [real_shape(n) for n in self.names], ""
        real = module.run_launcher
        with mock.patch.object(module, "run_launcher", launcher), mock.patch.object(module, "STATUS_SECONDS", 1), \
                mock.patch.object(module, "STATUS_PER_RECORD", 0.1), mock.patch.object(module, "_answers", lambda *a: True):
            lines, _ = self.status()
        timeouts = [l for l in lines if "did not finish" in l]
        self.assertEqual(len(timeouts), 0, timeouts[:1])
        self.assertTrue(seen, "the read was not made")

    def test_s10_sixty_records_show_no_timeout_reason_with_the_derived_timeout(self):
        self.check_read_timeouts()

    def test_s10_a_mutant_with_the_fixed_timeout_fails_the_sixty_record_test(self):
        module = self.cloud()
        applied = []

        def fixed(count):
            applied.append(True)
            return module.STATUS_SECONDS
        with mock.patch.object(module, "_read_timeout", fixed):
            with self.assertRaises(AssertionError):
                self.check_read_timeouts()
        self.assertTrue(applied, "the mutation was not applied")

    def test_s10_the_timeout_is_fifteen_plus_one_a_record_at_most_sixty(self):
        module = self.cloud()
        self.assertEqual([module._read_timeout(n) for n in (0, 1, 13, 45, 46, 300)], [15, 16, 28, 60, 60, 60])

    # ---- S6 mutants

    def test_s6_a_mutant_that_keeps_just_an_object_fails_s2(self):
        self.applied = False
        self.names = self.live(1)
        self.fakes(real_shape, drop_lists=True)
        lines, _ = self.status()
        line = [l for l in lines if "cloud w0" in l][0]
        self.assertTrue(self.applied, "the mutation was not applied")
        with self.assertRaises(AssertionError):
            self.assertNotIn("unavailable", line)

    def test_s6_a_mutant_that_reads_the_workers_one_after_another_fails_s3(self):
        module = self.cloud()
        applied = []

        def one_by_one(state, cluster, records, timeout, *more):
            applied.append(True)
            out = []
            for record in records:   # a read and a probe per worker, in turn
                time.sleep(0.3)
                out.append(module._probe_one(state, cluster, record, timeout))
            return out
        self.names = self.live(15)
        self.fakes(real_shape, delay=0.3)
        with mock.patch.object(module, "_probe_all", one_by_one):
            self.status()
        self.assertTrue(applied, "the mutation was not applied")
        with self.assertRaises(AssertionError):
            self.assertGreater(self.peak, 1, "the probes ran one after another")


DIRTY_TAIL = "\n" + "ghp_" + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJ" + " AKIA" + "ABCDEFGHIJKLMNOP"
NOT_TEXT = (5, None, ["a"], {"a": 1}, True)

# every free-text field a cloud status row takes from the launcher's record or answer: (field, where it comes from)
ROW_FIELDS = (("worker", "record"), ("runtime_id", "record"), ("hard_deadline", "record"), ("idle_deadline", "record"), ("session", "record"),
              ("phase", "answer"), ("idle_since", "answer"), ("cost_estimate_usd", "answer"), ("deadlines.idle", "answer"), ("error", "answer"),
              ("pod_phase", "pod"))


def dirty(field):
    return "\x1b[31m" + field + DIRTY_TAIL


class Scrubbed(Fakes, Base):
    """Every free-text field of a status row goes through the one helper, in the text row and in the JSON row, and a value that is not text raises nothing."""

    mine = contextlib.ExitStack()

    def row_with(self, field, source, value):
        """(text of the lines, JSON of the rows) of a status whose `field` holds `value`."""
        module = self.cloud()
        self.mine.close()   # the patches of an earlier field of the same test go
        self.mine = contextlib.ExitStack()
        self.addCleanup(self.mine.close)
        if source == "record":
            path = self.record("w0", hard=ALIVE)
            body = json.loads(path.read_text())
            body[field] = value   # the record is the launcher's untrusted file: it reaches the row as it stands (the loader's own checks aside)
            self.names = ["w0"]
            self.fakes(real_shape if field != "idle_deadline" else (lambda n: {k: v for k, v in real_shape(n).items() if k != "deadlines"}))
            self.mine.enter_context(mock.patch.object(module, "list_records", lambda state: [body]))
        elif source == "answer":
            self.names = self.live(1)

            def answer(name):
                body = real_shape(name)
                if field == "deadlines.idle":
                    body["deadlines"] = {"hard": ALIVE, "idle": value}
                else:
                    body[field] = value
                return body
            self.fakes(answer)
        else:   # the pod of a record past its hard deadline that still stands
            self.record("w0")
            self.names = ["w0"]
            self.fakes(real_shape)
            self.mine.enter_context(mock.patch.object(module, "_pod_of", lambda record, snapshot: {"name": "cw-w0", "phase": value}))
        lines, rows = self.status()
        return "\n".join(l for l in lines if l), json.dumps(rows), lines, rows

    def check_scrubbed(self, field, source):
        text, as_json, _, _ = self.row_with(field, source, dirty(field))
        for where, shown in (("text row", text), ("json row", as_json)):
            for bad in ("\x1b", "\\u001b", "ghp_", "AKIA"):
                self.assertNotIn(bad, shown, "%s: %s of the %s" % (field, repr(bad), where))

    def test_s8_every_field_of_a_row_is_scrubbed_in_the_text_row_and_the_json_row(self):
        for field, source in ROW_FIELDS:
            with self.subTest(field=field):
                self.check_scrubbed(field, source)

    def test_s8_a_value_that_is_not_text_raises_nothing_in_any_field(self):
        for field, source in ROW_FIELDS:
            for value in NOT_TEXT:
                with self.subTest(field=field, value=repr(value)):
                    text, as_json, lines, rows = self.row_with(field, source, value)
                    self.assertNotIn("unreadable", text)
                    self.assertIn("      cloud ", text.split("\n", 1)[-1])   # the row of the one worker is printed
                    self.assertEqual(len(rows), 1, lines)

    def test_s8_a_mutant_that_skips_the_scrub_for_one_field_fails_that_fields_test(self):
        module = self.cloud()
        real = module._safe
        for field, source in ROW_FIELDS:
            with self.subTest(field=field):
                applied = []

                def skipping(value, default="Unknown", kind=None, field=field):
                    if isinstance(value, str) and ("\x1b[31m" + field + "\n") in value:
                        applied.append(True)
                        return value   # the scrub is skipped for this field only
                    return real(value, default, kind)
                with mock.patch.object(module, "_safe", skipping):
                    with self.assertRaises(AssertionError):
                        self.check_scrubbed(field, source)
                self.assertTrue(applied, "the mutation was not applied")


class Lows(Fakes, Base):
    """S11: the record's problem and the launcher's standard error go through the one helper; a non-finite cost shows `?`."""

    def one_problem(self, text):
        module = self.cloud()
        self.names = ["w0"]
        self.fakes(real_shape)
        with mock.patch.object(module, "list_records", lambda state: [{"worker": "w0", "problem": text}]):
            lines, rows = self.status()
        return "\n".join(l for l in lines if l), json.dumps(rows)

    def one_stderr(self, text):
        module = self.cloud()
        self.names = self.live(1)
        self.fakes(real_shape)
        real = module.run_launcher
        stub = lambda runner, command, worker, *a, **k: (3, None, text) if command == "status" else real(runner, command, worker, *a, **k)
        with mock.patch.object(module, "run_launcher", stub):
            lines, rows = self.status()
        return "\n".join(l for l in lines if l), json.dumps(rows)

    def check_clean(self, shown):
        for bad in ("\x1b", "\\u001b", "ghp_", "AKIA"):
            self.assertNotIn(bad, shown[0] + shown[1])
        self.assertNotIn("\n      cloud w0: \n", shown[0])

    def test_s11_a_record_problem_with_an_escape_a_newline_and_a_secret_shape_shows_none_of_them(self):
        shown = self.one_problem(dirty("problem"))
        self.check_clean(shown)
        self.assertIn("cloud w0: ?", shown[0])

    def test_s11_a_clean_record_problem_is_kept(self):
        self.assertIn("cloud w0: the record is broken", self.one_problem("the record is broken")[0])

    def test_s11_the_launchers_standard_error_with_an_escape_a_newline_and_a_secret_shape_shows_none_of_them(self):
        shown = self.one_stderr(dirty("stderr"))
        self.check_clean(shown)
        self.assertIn("unavailable", shown[0])

    def test_s11_a_clean_launcher_error_is_kept(self):
        self.assertIn("boom", self.one_stderr("boom")[0])

    def test_s11_a_mutant_that_skips_the_helper_for_either_text_fails_that_texts_test(self):
        module = self.cloud()
        real = module._safe
        for name, check in (("problem", self.one_problem), ("stderr", self.one_stderr)):
            with self.subTest(text=name):
                applied = []

                def skipping(value, default="Unknown", kind=None, name=name):
                    if isinstance(value, str) and ("\x1b[31m" + name + "\n") in value:
                        applied.append(True)
                        return value
                    return real(value, default, kind)
                with mock.patch.object(module, "_safe", skipping):
                    with self.assertRaises(AssertionError):
                        self.check_clean(check(dirty(name)))
                self.assertTrue(applied, "the mutation was not applied")

    def check_cost_shows_a_question_mark(self, value):
        module = self.cloud()
        self.names = self.live(1)
        self.fakes(lambda n: dict(real_shape(n), cost_estimate_usd=value))
        lines, rows = self.status()
        line = [l for l in lines if "cloud w0" in l][0]
        self.assertIn("about $? so far", line)
        self.assertEqual(json.loads(json.dumps(rows))[0]["cost_estimate_usd"], "?")
        self.assertNotIn("NaN", json.dumps(rows))
        self.assertNotIn("Infinity", json.dumps(rows))

    def test_s11_a_cost_that_is_not_finite_prints_a_question_mark_in_the_text_and_the_json_row(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=repr(value)):
                self.check_cost_shows_a_question_mark(value)

    def test_s11_a_mutant_that_lets_a_non_finite_cost_through_fails(self):
        module = self.cloud()
        applied = []

        def loose(value):
            applied.append(True)
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        with mock.patch.object(module, "_finite_number", loose):
            with self.assertRaises(AssertionError):
                self.check_cost_shows_a_question_mark(float("nan"))
        self.assertTrue(applied, "the mutation was not applied")


class ErrorNotes(Fakes, Base):
    """S14: the strict text kind belongs to the status rows only. The error notes of create, stop, hub and the cluster read keep a non-ASCII
    character (an accented letter, a CJK character) and lose a control character, as before this change."""
    NOTE = "caf\u00e9 \u4e16\u754c failed"

    def shown(self, call):
        """The text a person sees from `call` (a function that raises or prints with the launcher's stderr set to NOTE + a control character)."""
        module = self.cloud()
        with mock.patch.object(module, "run_launcher", lambda *a, **k: (3, {"worker": "w0"} if a[1:2] == ("create",) else None, self.NOTE)):
            return call(module)

    def check_intact(self, text):
        self.assertIn("caf\u00e9 \u4e16\u754c failed", text)
        self.assertNotRegex(text, r"[\x00-\x1f\x7f]")   # the launcher run already made the note one line without control characters

    def test_s14_the_cluster_read_note_keeps_non_ascii(self):
        def call(module):
            with self.assertRaises(module.CloudError) as caught:
                module._cluster_snapshot(None, None, 5)
            return str(caught.exception)
        self.check_intact(self.shown(call))

    def test_s14_the_stop_note_keeps_non_ascii(self):
        def call(module):
            over = {"LaunchError": RuntimeError, "build_engine": lambda r, n, c: (None, None, None), "gates_caller_kind": lambda r, n: ("person", "x"),
                    "EXIT_OK": 0, "EXIT_ERROR": 1, "PROG": "sb"}
            with mock.patch.dict(module.H, over), mock.patch.object(module, "_may_retire", lambda k, r: True), mock.patch.object(module, "Cluster", mock.MagicMock), \
                    mock.patch.object(module, "_audit", lambda *a: None):
                with self.assertRaises(RuntimeError) as caught:
                    module.cmd_retire(types.SimpleNamespace(name="w0", reason="done", undo=False), None, None, {"runtime_id": "pod/w0"})
            return str(caught.exception)
        self.check_intact(self.shown(call))

    def test_s14_the_create_and_hub_notes_keep_non_ascii(self):
        """The create and the hub-session call sites of `_launch` take the note from `_reason` unwrapped (the create one is run, the hub one is read)."""
        module = self.cloud()
        for label in ("create", "hub-session"):
            with self.subTest(site=label):
                self.check_intact(module._reason(3, self.NOTE))
        source = Path(os.path.join(KIT, "bin", "cloud_worker.py")).read_text()
        for site in ('"the cloud worker was not created: %s" % _reason(code, note)', '"the hub entry could not be given the session: %s" % _reason(hcode, hnote)'):
            self.assertEqual(source.count(site), 1, site)

    def test_s14_a_mutant_that_applies_the_text_kind_to_the_note_shows_a_question_mark_at_every_site(self):
        module = self.cloud()
        applied = []
        real = module._reason

        def strict(code, note):
            applied.append(True)
            return real(code, module._safe(note, "?", "text") if note else note)
        with mock.patch.object(module, "_reason", strict):
            with self.assertRaises(AssertionError):
                self.test_s14_the_cluster_read_note_keeps_non_ascii()
            with self.assertRaises(AssertionError):
                self.test_s14_the_stop_note_keeps_non_ascii()
            with self.assertRaises(AssertionError):
                self.check_intact(module._reason(3, self.NOTE))
        self.assertGreaterEqual(len(applied), 3, "the mutation was not applied")

    def test_s14_a_status_row_with_the_same_text_still_passes_the_strict_kind(self):
        module = self.cloud()
        self.assertEqual(module._safe(self.NOTE, "?", "text"), "?")
        self.assertEqual(module._safe("plain note", "?", "text"), "plain note")

    def test_s14_a_failing_create_note_holds_the_text_intact(self):
        self.check_intact(self.cloud()._reason(3, self.NOTE))

    def test_s14_the_shared_redaction_table_is_reached_through_the_bound_namespace(self):
        module = self.cloud()
        for text in ("Authorization: Bearer " + "abcDEF123456" * 3, "password=hunter2xyz"):
            with self.subTest(text=text):
                self.assertEqual(module._safe(text, "?", "text"), "?")
        self.names = self.live(1)
        self.fakes(real_shape)
        real = module.run_launcher
        stub = lambda runner, command, worker, *a, **k: (3, None, "password=hunter2xyz") if command == "status" else real(runner, command, worker, *a, **k)
        with mock.patch.object(module, "run_launcher", stub):
            lines, rows = self.status()
        self.assertNotIn("hunter2xyz", "\n".join(l for l in lines if l) + json.dumps(rows))

    def test_s14_a_mutant_without_the_shared_table_lets_the_bearer_value_through(self):
        module = self.cloud()
        applied = []
        with mock.patch.dict(module.H, {"redact_command_text": lambda text, extra=(): applied.append(True) or text}):
            self.assertEqual(module._safe("password=hunter2xyz", "?", "text"), "password=hunter2xyz")
        self.assertTrue(applied, "the mutation was not applied")


class TestDocs(unittest.TestCase):
    def test_the_status_document_says_what_each_line_shows_and_the_lane_flag(self):
        flat = " ".join(Path(os.path.join(KIT, "docs", "switchboard.md")).read_text().split())
        for wanted in ("its idle deadline", "read ONCE for all workers", "four at a time", "status --lane NAME", "missing from that read"):
            self.assertIn(wanted, flat)

    def test_the_status_document_says_the_probe_stage_has_one_deadline_and_the_lows(self):
        flat = " ".join(Path(os.path.join(KIT, "docs", "switchboard.md")).read_text().split())
        section = flat[flat.index("The cloud workers in `status`"):][:6000]   # this change's paragraph
        self.assertEqual(section.count("however many"), 0)
        for wanted in ("the probe stage has one deadline (30 seconds) after which the remaining workers are listed as not probed",
                       "the launcher read has its own bound",
                       "the launcher answers a status for all workers four pods at a time",
                       "status --lane leaves out a record that has a problem (it has no recorded boss)",
                       "for a duplicate worker name in the launcher's list the last record wins",
                       "a runtime id or path is checked by its character set and length, not by its shape",
                       "the tunnel process of an abandoned probe lives until its own 120-second timeout",
                       "the launcher's status for all workers has one total deadline of 40 seconds, and a pod not read by then is shown with its idle time unknown and the rest of its record"):
            self.assertEqual(flat.count(wanted), 1, wanted)


if __name__ == "__main__":
    unittest.main()
