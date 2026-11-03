"""The #1786 standing launch queue (`fleet-switchboard queue`): S2-S7, S10, S11 and U1-U3. HandoffCase lives in the entry file: bin/test-switchboard passes it to build()."""
import argparse
import json
import os
from pathlib import Path
from support.loader import SWITCHBOARD_SOURCE_FILES, captured, sb

MARK = "# ---- queue (#1786)"
H1, H2, H3 = ["sha256:" + c * 64 for c in "abc"]   # the real format: `plan check --json` and the plan.approved event carry the prefix
ISSUE = "o/r#9"


def section_text():
    for path in SWITCHBOARD_SOURCE_FILES:
        text = path.read_text()
        if MARK in text:
            return text[text.index(MARK):]
    raise AssertionError("the section %s is not in the daemon's files" % MARK)


def build(HandoffCase):
    class TestLaunchQueue(HandoffCase):
        """#1786: one queue file the lanes append to and the cloud lane reads at a free slot."""

        def setUp(self):
            super().setUp()
            self.state = str(self.world.state)
            self.cloud = self.world.add_agent("fleet-cloud")
            self.other = self.world.add_agent("other")
            self.n = 0

        def as_lane(self, pane):
            os.environ["HERDR_PANE_ID"] = self.world.panes[pane]

        def queue_path(self, name="launch-queue.jsonl"):
            return Path(self.state) / name

        def queue_lines(self, name="launch-queue.jsonl"):
            path = self.queue_path(name)
            return path.read_text().splitlines() if path.exists() else []

        def audit_events(self, event):
            return [e for e in self.world.audit() if e["event"] == event]

        def run_queue(self, *argv, ns=None):
            with captured() as (out, err):
                if ns is None:
                    code = sb.main(["queue"] + list(argv), runner=self.route, now=self.world.clock.now, socket_call=self.world.herdr.socket)
                else:
                    parser = argparse.ArgumentParser()
                    ns["add_queue_parsers"](parser.add_subparsers(dest="action"))
                    code = ns["cmd_queue"](parser.parse_args(["queue"] + list(argv)), self.route, self.world.clock.now)
            return code, out.getvalue()

        def put(self, name, priority=1, kind="builder", plan_hash=H1, lane="cos", **more):
            self.n += 1
            fields = {"name": name, "kind": kind, "ask": 9, "brief_file": "/tmp/brief-%s.md" % name, "priority": priority,
                      "plan_issue": ISSUE, "plan_hash_full": plan_hash}
            fields.update(more)
            key = sb.queue_add(self.state, lane, fields, self.world.clock.now())
            self.world.tick(60)
            return key

        def approved(self, *hashes):
            for one in hashes:
                sb.AuditLog(Path(self.state) / "audit.jsonl").append("daemon", "plan.approved", ISSUE, {"issue": ISSUE, "plan_hash": one})

        def approved_as(self, issue, one):
            sb.AuditLog(Path(self.state) / "audit.jsonl").append("daemon", "plan.approved", issue, {"issue": issue, "plan_hash": one})

        def next_lines(self, free, live=H1, ns=None):
            fn = (ns or vars(sb))["queue_next"]
            return fn(self.state, free, self.world.clock.now(), lambda issue: live, sb.queue_approvals(self.state))[0]

        def printed(self, lines):
            return [json.loads(line)["name"] for line in lines if line.startswith("{")]

        def mutated(self, old, new):
            text = section_text()
            self.assertIn(old, text)
            ns = dict(vars(sb))
            exec(compile(text.replace(old, new), "mutant-queue", "exec"), ns)
            return ns

        def check_order(self, ns=None, tag=""):
            first, second, third = self.put("p2" + tag, 2), self.put("p1a" + tag, 1), self.put("p1b" + tag, 1)
            self.approved(H1)
            got = self.printed(self.next_lines(1, ns=ns))
            if got != ["p1a" + tag]:
                raise AssertionError("the first printed is not the oldest of the lowest priority: %r" % got)
            return first, second, third

        def test_s2_one_add_is_one_bounded_line_and_a_second_add_is_refused(self):
            brief = Path(self.state) / "brief.md"
            brief.write_text("SECRET-BRIEF-TEXT")
            argv = ["add", "--name", "pc1", "--kind", "builder", "--ask", "9", "--brief-file", str(brief), "--base-branch", "integration",
                    "--wip-branch", "wip/pc1", "--role", "worker", "--reports-to", "cos", "--shape", "M", "--idle", "30", "--hard", "60",
                    "--priority", "1", "--plan-issue", ISSUE, "--plan-hash-full", H1, "--approved-by", "chief", "--approved-at", "2026-11-01T10:00:00Z"]
            code, out = self.run_queue(*argv)
            self.assertEqual(code, 0, out)
            (line,) = self.queue_lines()
            record = json.loads(line)
            self.assertLessEqual(set(record), set(sb.QUEUE_KEYS))
            self.assertLessEqual(len(set(record)), 20)
            self.assertLessEqual(len(line.encode()), 2000)
            self.assertEqual(record["lane"], "cos")
            self.assertNotIn("SECRET-BRIEF-TEXT", line)
            code, out = self.run_queue(*argv)
            self.assertEqual(code, 1)
            self.assertEqual(len(out.strip().splitlines()), 1)
            self.assertEqual(len(self.queue_lines()), 1)
            self.put("nohash", plan_hash=None)
            lines = self.next_lines(5)
            reasons = [json.loads(s) for s in self.queue_lines("launch-queue.status.jsonl")]
            self.assertEqual([r["reason"] for r in reasons if r["reason"] == "no-plan-hash"], ["no-plan-hash"])
            self.assertNotIn("nohash", self.printed(lines))

        def test_s3_the_lowest_priority_number_oldest_first_launches_and_two_remain_in_order(self):
            first, second, third = self.check_order()
            self.as_lane("fleet-cloud")
            code, out = self.run_queue("status", second, "launched", "--pod", "pod-a")
            self.assertEqual(code, 0, out)
            code, out = self.run_queue("list")
            ids = [line.split()[0] for line in out.splitlines()]
            self.assertEqual(ids, [third, first])

        def test_s4_a_stale_hash_is_refused_with_one_line_and_the_next_entry_is_printed(self):
            self.put("old", plan_hash=H2)
            self.put("good", plan_hash=H1)
            self.approved(H1, H2)
            lines = self.next_lines(1)
            self.assertEqual([l for l in lines if l.startswith("not launched")],
                             ["not launched: old: the live plan hash %s is not the one the entry needs %s" % (H1[7:15], H2[7:15])])
            self.assertEqual(self.printed(lines), ["good"])
            self.assertEqual(len([l for l in self.run_queue("list")[1].splitlines() if " old " in l]), 1)

        def test_s5_only_the_lane_that_queued_changes_and_a_stale_entry_is_never_printed(self):
            key = self.put("mine", priority=3)
            self.assertEqual(self.run_queue("priority", key, "1")[0], 0)
            self.assertEqual(self.run_queue("cancel", key)[0], 0)
            self.assertEqual([e["event"] for e in self.world.audit() if e["event"].startswith("queue.")], ["queue.priority", "queue.cancel"])
            self.assertEqual(self.audit_events("queue.priority")[0]["detail"]["old"], 3)
            self.assertEqual(self.audit_events("queue.priority")[0]["detail"]["new"], 1)
            kept = self.put("kept")
            before = self.queue_lines()
            self.as_lane("other")
            for argv in (("priority", kept, "2"), ("cancel", kept)):
                code, out = self.run_queue(*argv)
                self.assertEqual(code, 1)
                self.assertEqual(len(out.strip().splitlines()), 1)
            self.assertEqual(self.queue_lines(), before)
            self.approved(H1)
            self.world.tick(121 * 60)
            lines = self.next_lines(3)
            self.assertEqual(self.printed(lines), [])
            self.assertEqual(len([l for l in lines if l.startswith("stale: kept, queued ")]), 1)
            self.assertIn(" stale", self.run_queue("list")[1])

        def test_s6_no_free_slot_prints_nothing_and_changes_nothing(self):
            for name in ("a", "b", "c"):
                self.put(name)
            self.approved(H1)
            before, audit = self.queue_path().read_bytes(), len(self.world.audit())
            code, out = self.run_queue("next", "--free", "0")
            self.assertEqual((code, out), (0, ""))
            self.assertEqual(self.next_lines(0), [])
            self.assertEqual(self.queue_path().read_bytes(), before)
            self.assertFalse(self.queue_path("launch-queue.status.jsonl").exists())
            self.assertEqual(len(self.world.audit()), audit)

        def test_s7_the_unmutated_copy_passes_and_a_newest_first_mutant_fails_with_the_text(self):
            self.check_order()   # the unmutated copy passes first
            fresh = self.mutated('order=(priority, queued or 0, key)', 'order=(priority, -(queued or 0), key)')
            with self.assertRaises(AssertionError) as caught:
                self.check_order(fresh, "m")
            self.assertIn("the first printed is not the oldest of the lowest priority", str(caught.exception))

        def test_s10_a_builder_needs_an_approval_event_not_the_approved_by_key(self):
            self.put("forged", approved_by="chief-of-staff", approved_at="2026-11-02T01:00:00Z")
            self.put("tester", kind="tester", plan_hash=None, plan_issue=None)

            def check(ns=None, tag=""):
                if tag:
                    self.put("forged" + tag, approved_by="chief-of-staff")
                lines = self.next_lines(5, ns=ns)
                if "forged" + tag in self.printed(lines):
                    raise AssertionError("the entry was printed without an approval event")
                return lines
            lines = check()   # the unmutated copy passes first
            self.assertEqual(self.printed(lines), ["tester"])
            self.assertEqual([json.loads(s)["reason"] for s in self.queue_lines("launch-queue.status.jsonl")], ["no-approval"])
            mutant = self.mutated('if (item.get("plan_issue"), full) not in approvals:', 'if not item.get("approved_by"):')
            with self.assertRaises(AssertionError) as caught:
                check(mutant, "m")
            self.assertIn("the entry was printed without an approval event", str(caught.exception))
            self.approved(H1)
            replacement = {"name": "forged", "kind": "builder", "ask": 9, "brief_file": "/tmp/b.md", "priority": 1, "plan_issue": ISSUE, "plan_hash_full": H1}
            entries = sb.queue_fold(self.state)[0]
            sb.queue_append(self.queue_path(), dict(replacement, id=next(iter(entries)), lane="cos", enqueued=sb.iso(self.world.clock.now())))
            self.assertIn("forged", self.printed(self.next_lines(5)))

        def test_s11_one_wake_line_and_a_refused_wake_never_fails_the_add(self):
            sent = []
            real = sb.Switchboard.send

            def fake(engine, sender, recipient, text, **kw):
                sent.append((recipient, text))
            sb.Switchboard.send = fake
            try:
                code, out = self.run_queue("add", "--name", "w1", "--kind", "tester", "--ask", "9", "--brief-file", "/tmp/b.md", "--priority", "2")
            finally:
                sb.Switchboard.send = real
            self.assertEqual(code, 0, out)
            (line,) = self.queue_lines()
            self.assertEqual(sent, [("fleet-cloud", "entry %s queued" % json.loads(line)["id"])])

            def refused(engine, *a, **kw):
                raise sb.FactError("the send is refused")

            def check(ns=None):
                sb.Switchboard.send = refused
                try:
                    code, out = self.run_queue("add", "--name", "w2", "--kind", "tester", "--ask", "9", "--brief-file", "/tmp/b.md", ns=ns)
                except Exception:
                    raise AssertionError("the add failed because the wake failed") from None
                finally:
                    sb.Switchboard.send = real
                if code != 0:
                    raise AssertionError("the add failed because the wake failed")
                return out
            out = check()   # the unmutated copy passes first
            self.assertIn("the wake failed", out)
            self.assertEqual(len(self.queue_lines()), 2)
            mutant = self.mutated("except Exception:   # the entry is written", "except ZeroDivisionError:   # the entry is written")
            with self.assertRaises(AssertionError) as caught:
                check(mutant)
            self.assertIn("the add failed because the wake failed", str(caught.exception))

        def test_s12_real_format_hash_is_admitted_end_to_end(self):
            real = "sha256:" + "d" * 64
            done = argparse.Namespace(stdout=json.dumps({"result": "pass", "plan_hash": real, "source": ISSUE}) + "\n")
            self.assertEqual(sb.queue_live_hash(lambda argv, **kw: done, ISSUE), real)
            self.assertEqual(sb.queue_hash(real), sb.queue_hash("d" * 64))
            self.assertIsNone(sb.queue_hash("sha256:" + "d" * 63))
            self.put("real", plan_hash=real)
            self.approved(real)
            lines = self.next_lines(1, live=real)
            self.assertEqual(self.printed(lines), ["real"], lines)
            self.assertEqual(self.queue_lines("launch-queue.status.jsonl"), [])

        def test_s13_plan_issue_is_owner_repo_number_or_refused_with_a_name(self):
            base = ["add", "--name", "pi", "--kind", "builder", "--ask", "9", "--brief-file", "/tmp/b.md", "--plan-hash-full", H1]
            code, out = self.run_queue(*base, "--plan-issue", "1786")
            self.assertEqual(code, 1)
            self.assertIn("plan_issue 1786 is a bare number", out)
            self.assertEqual(self.queue_lines(), [])
            code, out = self.run_queue(*base, "--plan-issue", "1786", "--repo", "adamkaplan/fleet-kit")
            self.assertEqual(code, 0, out)
            self.assertEqual(json.loads(self.queue_lines()[0])["plan_issue"], "adamkaplan/fleet-kit#1786")
            self.assertEqual(self.run_queue(*base[:2], "pj", *base[3:], "--plan-issue", "x y")[0], 1)
            # the audit key of the real event is owner/repo#N: the normalised entry meets it
            self.approved_as("adamkaplan/fleet-kit#1786", H1)
            self.assertEqual(self.printed(self.next_lines(1, live=H1)), ["pi"])

        def test_s14_live_hash_failures_are_named_in_the_refusal_line(self):
            def ran(text, code=0):
                return lambda argv, **kw: argparse.Namespace(stdout=text, returncode=code)

            def boom(argv, **kw):
                raise OSError("no such file")
            for runner, cause in ((boom, "OSError"), (ran("not json"), "not json"), (ran(json.dumps({"result": "error", "error": "no plan"}), 2), "no plan"),
                                  (ran(json.dumps({"result": "pass"})), "no plan_hash")):
                with self.assertRaises(sb.FactError) as caught:
                    sb.queue_live_hash(runner, ISSUE)
                self.assertIn(cause, str(caught.exception))
            self.put("unread")
            self.approved(H1)

            def failing(issue):
                raise sb.FactError("plan check says: no plan")
            out, _ = sb.queue_next(self.state, 1, self.world.clock.now(), failing, sb.queue_approvals(self.state))
            self.assertEqual(out, ["not launched: unread: the live plan hash could not be read: plan check says: no plan"])
            (status,) = [json.loads(l) for l in self.queue_lines("launch-queue.status.jsonl")]
            self.assertEqual((status["state"], status["reason"]), ("waiting", "hash-unread: plan check says: no plan"))
            sb.queue_next(self.state, 1, self.world.clock.now(), failing, sb.queue_approvals(self.state))
            self.assertEqual(len(self.queue_lines("launch-queue.status.jsonl")), 1)
            self.assertEqual(self.printed(self.next_lines(1)), ["unread"])

        def test_s15_priority_status_and_ask_are_checked(self):
            key = self.put("chk", kind="tester", plan_hash=None, plan_issue=None)
            before = self.queue_lines()
            for bad in ("-1", "101"):
                code, out = self.run_queue("priority", key, bad)
                self.assertEqual(code, 1, bad)
                self.assertIn("priority is a whole number from 0 to 100", out)
            self.assertEqual(self.queue_lines(), before)
            code, out = self.run_queue("add", "--name", "noask", "--kind", "tester", "--brief-file", "/tmp/b.md")
            self.assertEqual((code, out.strip()), (1, "queue: --ask is required: the issue number the pod works under"))
            self.assertEqual(self.queue_lines(), before)
            self.as_lane("fleet-cloud")
            for argv, text in ((("status", key, "bogus"), "state is one of"), (("status", "q-none", "waiting"), "no queued entry q-none")):
                code, out = self.run_queue(*argv)
                self.assertEqual(code, 1, argv)
                self.assertIn(text, out)
            self.assertEqual(self.queue_lines("launch-queue.status.jsonl"), [])
            self.assertEqual(self.run_queue("status", key, "refused", "--reason", "base-missing")[0], 0)
            self.as_lane("cos")
            code, out = self.run_queue("priority", key, "2")
            self.assertEqual(code, 0)
            self.assertIn("note: %s is refused (base-missing)" % key, out)
            code, out = self.run_queue("cancel", key)
            self.assertIn("note: %s is refused (base-missing)" % key, out)

        def test_u1_unreadable_lines_are_counted_and_named_and_a_missing_file_is_empty(self):
            self.assertEqual(self.run_queue("list"), (0, ""))
            good = lambda n: json.dumps({"id": "q-%d" % n, "enqueued": sb.iso(self.world.clock.now()), "lane": "cos", "name": "n%d" % n, "kind": "tester", "priority": n})
            rows = [good(1), "{not json", good(2), json.dumps({"x": 1}), json.dumps({"id": "q-3", "name": "z" * 2100})]
            self.queue_path().write_text("\n".join(rows) + "\n")
            for argv in (("list",), ("next", "--free", "2")):
                code, out = self.run_queue(*argv)
                self.assertEqual(code, 0, out)
            code, out = self.run_queue("list")
            self.assertEqual([l for l in out.splitlines() if l.startswith("unreadable")], ["unreadable: line 2", "unreadable: line 4", "unreadable: line 5"])
            self.assertEqual(len([l for l in out.splitlines() if l.startswith("q-")]), 2)

        def test_u2_the_section_starts_no_pod(self):
            text = section_text()
            for word in ("kubectl", "cloud_worker", "run_cloud", "subprocess", "cmd_launch", "Popen"):
                self.assertNotIn(word, text)

        def test_u3_the_section_reads_json_through_the_bounded_parse_and_adds_no_import(self):
            text = section_text()
            self.assertNotIn("json.loads", text)
            self.assertNotIn("import ", text)
            self.assertIn('load_module("record_json").parse_bounded(', text)
            self.assertIn("queue", sb.build_parser().format_help())
    return TestLaunchQueue
