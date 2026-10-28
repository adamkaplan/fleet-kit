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
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_cloud_status_summary import ALIVE, CLOUD, KIT, Base, Path   # noqa: E402  (the same loader and fakes)


def real_shape(name, phase="Running"):
    """What `cloud-worker status NAME --json` really prints for one worker: an object inside a list."""
    return {"cost_estimate_usd": 0.42, "deadlines": {"hard": ALIVE, "idle": "2026-10-28T12:00:00Z"}, "idle_since": None, "phase": phase,
            "runtime_id": "pod/cw-" + name, "worker": name}


class Bounded(Base):
    DELAY = 0.0

    def setUp(self):
        super().setUp()
        self.reads, self.probes = [], []
        self.lock = threading.Lock()

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
            time.sleep(delay)
            return True

        stack = [mock.patch.object(module, "run_launcher", launcher), mock.patch.object(module, "_answers", probe)]
        for patch in stack:
            patch.start()
            self.addCleanup(patch.stop)

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
        self.fakes(real_shape, delay=1.5)
        started = time.monotonic()
        lines, rows = self.status()
        took = time.monotonic() - started
        print("s3 took %.1f s" % took, file=sys.stderr)
        self.assertLess(took, 30, took)
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

        def one_by_one(state, cluster, records, timeout):
            applied.append(True)
            out = []
            for record in records:   # a read and a probe per worker, in turn
                time.sleep(1.5)
                out.append(module._probe_one(state, cluster, record, timeout))
            return out
        self.names = self.live(15)
        self.fakes(real_shape, delay=1.5)
        with mock.patch.object(module, "_probe_all", one_by_one):
            started = time.monotonic()
            self.status()
            took = time.monotonic() - started
        self.assertTrue(applied, "the mutation was not applied")
        with self.assertRaises(AssertionError):
            self.assertLess(took, 30, took)


DIRTY_TAIL = "\n" + "ghp_" + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJ" + " AKIA" + "ABCDEFGHIJKLMNOP"
NOT_TEXT = (5, None, ["a"], {"a": 1}, True)

# every free-text field a cloud status row takes from the launcher's record or answer: (field, where it comes from)
ROW_FIELDS = (("worker", "record"), ("runtime_id", "record"), ("hard_deadline", "record"), ("idle_deadline", "record"), ("session", "record"),
              ("phase", "answer"), ("idle_since", "answer"), ("cost_estimate_usd", "answer"), ("deadlines.idle", "answer"), ("error", "answer"),
              ("pod_phase", "pod"))


def dirty(field):
    return "\x1b[31m" + field + DIRTY_TAIL


class Scrubbed(Bounded):
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


class TestDocs(unittest.TestCase):
    def test_the_status_document_says_what_each_line_shows_and_the_lane_flag(self):
        flat = " ".join(Path(os.path.join(KIT, "docs", "switchboard.md")).read_text().split())
        for wanted in ("its idle deadline", "read ONCE for all workers", "four at a time", "status --lane NAME", "missing from that read"):
            self.assertIn(wanted, flat)


if __name__ == "__main__":
    unittest.main()
