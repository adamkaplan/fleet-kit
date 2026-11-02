"""`fleet-switchboard issue-line` (ask #1751, pull request A): one line of an issue body changed from a fresh read, refused when the read is empty,
failed or lacks the Intent heading or the Done-when line; the last good copy; the restore.

HOST is bin/test-switchboard (set by its loader line). The fake GitHub is that file's FakeGH; each read and each write is counted. A mutant is the real
module with one function replaced, asserted applied; its test must fail with the anchored text, and the same test on the unmutated command passes.
"""
import json
import os
import re
import stat
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST = None   # set by bin/test-switchboard

BODY = ("Why this ask exists.\n\n## Intent\nFix the agents.\nDone when: both answer.\n\n## Notes\nOther paragraph.\n\n"
        "Next step (1 Nov 05:00Z): wait for review.\nParked until: 2027-03-01\n\n<!-- fleet-switchboard:from=fleet-registry -->\n")
BODY_NO_NEXT = "Why.\n\n## Intent\nFix it.\nDone when: ok.\n\nA closing paragraph.\n\n<!-- fleet-switchboard:from=fleet-registry -->\n"
WRITE_FAILED = "wrote %d times where 0 were expected"


def build():
    host = HOST
    sb = host.sb

    class TestIssueLine(host.IntentCase):
        """S2-S6 and U1, U2: the command on the fake GitHub, the module's copy and the mutants."""

        def setUp(self):
            super().setUp()
            self.write_config({"opencode": host.FAKE_OPENCODE, "herdr": host.FAKE_HERDR, "gh": host.FAKE_GH, "intent_repo": host.FAKE_REPO})
            self.gh.bodies = {5: BODY}
            self.ref = "%s#5" % host.FAKE_REPO
            self.module = sb.load_module("issue_line")
            self.hook, self.reads = None, []
            patcher = mock.patch.object(sb, "ORDERS_READBACK_WAITS", (0, 0, 0))
            patcher.start()
            self.addCleanup(patcher.stop)

        def run_it(self, *argv):
            def route(call, **kwargs):
                if call[0] == host.FAKE_GH and len(call) == 3 and call[2].endswith("/issues/5"):
                    self.reads.append(call)
                    if self.hook:
                        self.hook(len(self.reads))
                return self.gh(call, **kwargs) if call[0] == host.FAKE_GH else self.world.route(call, **kwargs)
            with host.captured() as (out, err):
                code = sb.main(["issue-line"] + list(argv), runner=route, now=self.world.clock.now, wait=lambda sock, s: False,
                               socket_call=self.world.herdr.socket)
            return code, out.getvalue(), err.getvalue()

        def writes(self):
            return getattr(self.gh, "patches", [])

        def copies(self):
            return sorted((self.world.state / "issue-line").glob("*.json"))

        def audit_text(self):
            return self.world.audit_path.read_text() if self.world.audit_path.exists() else ""

        def fresh(self):
            self.gh.down, self.gh.bodies, self.reads = False, {5: BODY}, []
            self.gh.__dict__.pop("patches", None)

        def refused(self, *argv, expect=None):
            """The one check of every refusal: exit status 1, one plain line, no write."""
            code, _, err = self.run_it(*argv)
            if len(self.writes()) != 0:
                if expect:
                    raise AssertionError("the refusal /%s/ was not held: wrote %d times where 0 were expected" % (expect, len(self.writes())))
                raise AssertionError(WRITE_FAILED % len(self.writes()))
            self.assertEqual(code, 1, err)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            if expect and not re.search(expect, err):
                raise AssertionError("the refusal line does not say /%s/: %s" % (expect, err.strip()))

        def mutated(self, name, replacement, check, anchor=r"^wrote [0-9]+ times where 0 were expected"):
            """Apply the mutant (asserted applied), run `check`: it must fail with the check's own text (`anchor`)."""
            original = getattr(self.module, name)
            with mock.patch.object(self.module, name, replacement):
                self.assertIsNot(getattr(self.module, name), original, "the mutant was not applied")
                with self.assertRaisesRegex(AssertionError, anchor):
                    check()

        # -- S2: the three refusals, each with a mutant and the control

        def empty_read(self):
            self.gh.bodies = {5: ""}

        def failed_read(self):
            self.gh.down = True

        def no_heading(self):
            self.gh.bodies = {5: "Done when: ok.\n\nNext: x\n"}

        def case_refusals(self):
            return (("empty read", self.empty_read, "empty"), ("failed read", self.failed_read, "could not be read"),
                    ("no Intent heading", self.no_heading, "no `## Intent` heading"))

        def test_s2_the_three_refusals_write_nothing(self):
            for label, make, text in self.case_refusals():
                with self.subTest(label):
                    self.fresh()
                    make()
                    self.refused(self.ref, "--next-step", "wait", expect=text)

        def read_that_goes_on(self):
            """The failed-read mutant: catches the read error, brings the fake up and goes on with a good body, so the command writes (the write is counted)."""
            original = self.module._read

            def read(sb, repo, number):
                try:
                    return original(sb, repo, number)
                except sb.IntentError:
                    self.gh.down = False
                    return BODY
            return read

        def test_s2_each_mutant_that_writes_anyway_is_caught_and_the_control_passes(self):
            for label, make, text in self.case_refusals():
                with self.subTest(label):
                    self.fresh()
                    make()
                    self.refused(self.ref, "--next-step", "wait", expect=text)   # the control: the unmutated command passes
                    self.gh.__dict__.pop("patches", None)
                    anchor = r"^the refusal /%s/ was not held: wrote [0-9]+ times" % re.escape(text)   # names the mutated check
                    check = lambda: self.refused(self.ref, "--next-step", "wait", expect=text)
                    if label == "failed read":
                        self.mutated("_read", self.read_that_goes_on(), check, anchor)
                    else:
                        self.mutated("body_problem", lambda body: None, check, anchor)
                    self.gh.__dict__.pop("patches", None)

        # -- S3: the copy

        def put_copy(self, raw):
            directory = self.world.state / "issue-line"
            directory.mkdir(mode=0o700, exist_ok=True)
            self.run_it(self.ref, "--next-step", "seed")   # creates the copy for this issue, so its name is known
            (path,) = self.copies()
            self.gh.__dict__.pop("patches", None)
            self.gh.bodies = {5: BODY}
            path.write_bytes(raw) if isinstance(raw, bytes) else None
            return path

        GOOD = b'{"intent": true, "done_when": true, "at": 1}'

        def test_s3_a_body_without_done_when_is_refused(self):
            self.gh.bodies = {5: "## Intent\nFix it.\n\nNext: x\n"}
            self.refused(self.ref, "--next-step", "wait", expect="no `Done when:` line")

        def test_s3_a_copy_that_contradicts_a_corrupt_oversized_and_unreadable_copy_are_refused(self):
            path = self.put_copy(self.GOOD)
            self.gh.bodies = {5: "Done when: ok.\n"}   # the body read now lacks the Intent heading, the copy says it held it
            self.refused(self.ref, "--next-step", "wait", expect="the last good read held both parts")
            self.gh.bodies = {5: BODY}
            for label, raw, text in (("corrupt", b"{not json", "corrupt"), ("oversized", b" " * 5000 + self.GOOD, "oversized"),
                                     ("extra key", b'{"intent": true, "done_when": true, "at": 1, "body": "x"}', "corrupt")):
                with self.subTest(label):
                    path.write_bytes(raw)
                    self.refused(self.ref, "--next-step", "wait", expect=text)
            path.write_bytes(self.GOOD)
            path.chmod(0)
            if os.access(str(path), os.R_OK):
                path.chmod(0o600)
                self.skipTest("this account reads a mode 0 file")
            try:
                self.refused(self.ref, "--next-step", "wait", expect="cannot be read")
            finally:
                path.chmod(0o600)

        def test_s3_a_missing_copy_with_both_parts_proceeds_and_writes_the_copy(self):
            self.assertEqual(self.copies(), [])
            code, _, err = self.run_it(self.ref, "--next-step", "wait for the owner")
            self.assertEqual((code, err, len(self.writes()), len(self.copies())), (0, "", 1, 1))

        def test_s3_mutants_that_ignore_the_copy_or_read_a_corrupt_copy_as_absent_are_caught(self):
            path = self.put_copy(self.GOOD)
            self.gh.bodies = {5: "Done when: ok.\n"}
            self.refused(self.ref, "--next-step", "wait", expect="the last good read held both parts")   # control
            with mock.patch.object(self.module, "read_copy", lambda sb_, repo, number: None):
                with self.assertRaisesRegex(AssertionError, r"^the refusal line does not say /the last good read held both parts/"):   # the command's own refusal line
                    self.refused(self.ref, "--next-step", "wait", expect="the last good read held both parts")
            self.gh.bodies = {5: BODY}
            path.write_bytes(b"{not json")
            self.refused(self.ref, "--next-step", "wait", expect="corrupt")   # control

            def absent(sb_, repo, number):
                try:
                    return real(sb_, repo, number)
                except self.module.Refused:
                    return None
            real = self.module.read_copy
            self.mutated("read_copy", absent, lambda: self.refused(self.ref, "--next-step", "wait", expect="corrupt"),
                         r"^the refusal /corrupt/ was not held: wrote [0-9]+ times")

        # -- S4: only that line differs

        def test_s4_only_the_changed_line_differs_and_the_daemon_readers_accept_it(self):
            before = BODY
            code, _, err = self.run_it(self.ref, "--next-step", "waiting for the review on 2099-01-05")
            self.assertEqual((code, err), (0, ""))
            after = self.gh.body(5)
            old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
            self.assertEqual(len(old), len(new))
            differing = [n for n in range(len(old)) if old[n] != new[n]]
            self.assertEqual(len(differing), 1)
            self.assertTrue(new[differing[0]].startswith("Next: "))
            self.assertEqual(before[:before.index("Next step")], after[:before.index("Next step")])   # the Intent section and the Done-when line byte-equal
            self.assertIn("## Intent\nFix the agents.\nDone when: both answer.\n", after)
            self.assertEqual(self.reader_accepts(after), (True, True, True))   # 3 of 3 daemon readers
            self.gh.__dict__.pop("patches", None)
            future = time.strftime("%Y-%m-%d", time.gmtime(self.world.clock.now() + 40 * 86400))
            code, _, err = self.run_it(self.ref, "--parked", future)
            self.assertEqual((code, err), (0, ""))
            final = self.gh.body(5)
            old, new = after.splitlines(keepends=True), final.splitlines(keepends=True)
            self.assertEqual([old[n] for n in range(len(old)) if old[n] != new[n]], ["Parked until: 2027-03-01\n"])
            self.assertIn("Parked until: %s\n" % future, final)
            self.assertIn("## Intent\nFix the agents.\nDone when: both answer.\n", final)

        def reader_accepts(self, body):
            """(standing_line reads the next-step line, the parked reader reads a parked line)"""
            nxt = [l for l in body.splitlines() if l.startswith("Next: ")][-1]
            standing = sb.standing_line(nxt, self.world.clock.now())
            parked = sb.parked_until(body, ["x:parked"])
            return (sb.TRIAGE_NEXT_RE.search(nxt) is not None, standing is not None and standing[0] == "date", parked is not None)

        def test_s4_a_body_with_no_next_step_gets_one_before_the_trailing_marker(self):
            self.gh.bodies = {5: BODY_NO_NEXT}
            self.assertEqual(self.run_it(self.ref, "--next-step", "start")[0], 0)
            after = self.gh.body(5)
            self.assertTrue(re.search(r"A closing paragraph\.\n\nNext: start\n\n<!-- fleet-switchboard:from=fleet-registry -->\n$", after), after)
            self.assertTrue(after.startswith(BODY_NO_NEXT[:BODY_NO_NEXT.index("<!--")].rstrip("\n")))

        def test_s4_the_written_line_is_read_by_standing_line(self):
            line = "Next: wait for 2099-01-05"
            self.assertEqual(sb.standing_line(line, 0)[0], "date")
            self.assertIsNotNone(sb.TRIAGE_NEXT_RE.search(line))

        # -- S5: the second read and the restore

        def test_s5_a_body_that_moved_between_the_reads_is_refused(self):
            moved = BODY.replace("wait for review", "something else")

            def move(count):
                if count == 2:
                    self.gh.bodies = {5: moved}
            self.hook = move
            self.refused(self.ref, "--next-step", "wait", expect="changed while this edit was prepared")
            self.assertEqual(self.gh.bodies[5], moved)

        def lose_parts_after_the_write(self):
            def lose(count):
                if count == 3:
                    self.gh.bodies = {5: "Next: wait\n"}
            self.hook = lose

        def check_restore(self):
            code, _, err = self.run_it(self.ref, "--next-step", "wait")
            if len(self.writes()) != 2:
                raise AssertionError("wrote %d times where 2 were expected (the edit and one restore)" % len(self.writes()))
            self.assertEqual((code, self.writes()[1]["body"]), (1, BODY))
            self.assertIn("issue_line.restored", self.audit_text())
            self.assertNotIn("wait\"", self.audit_text())

        def test_s5_a_write_read_back_without_the_intent_is_restored_once_byte_equal(self):
            self.lose_parts_after_the_write()
            self.check_restore()

        def test_s5_mutants_without_the_second_read_or_the_restore_are_caught(self):
            real = self.module._read
            moved = BODY.replace("wait for review", "something else")
            state = {"n": 0}

            def skip_second(sb_, repo, number):
                state["n"] += 1
                return real(sb_, repo, number) if state["n"] != 2 else BODY   # the "second read" returns the first body unread

            def move(count):
                if count == 2:
                    self.gh.bodies = {5: moved}
            self.hook = move
            self.refused(self.ref, "--next-step", "wait", expect="changed while")   # control
            self.gh.__dict__.pop("patches", None)
            self.gh.bodies, self.reads = {5: BODY}, []
            self.mutated("_read", skip_second, lambda: self.refused(self.ref, "--next-step", "wait"))
            self.gh.__dict__.pop("patches", None)
            self.gh.bodies, self.reads, self.hook = {5: BODY}, [], None
            self.lose_parts_after_the_write()
            with mock.patch.object(self.module, "parts", lambda body: (True, True)) as applied:
                self.assertIsNotNone(applied)
                with self.assertRaisesRegex(AssertionError, r"^wrote 1 times where 2 were expected"):
                    self.check_restore()

        # -- S14: a restore write that fails is not silent

        def check_restore_failed(self):
            self.lose_parts_after_the_write()
            seen = []

            def refuse_the_second(number):
                seen.append(number)
                if len(seen) == 2:
                    raise sb.IntentError("restore refused")
            self.gh.before_patch = refuse_the_second
            code, _, err = self.run_it(self.ref, "--next-step", "wait")
            if "restore of the old body failed" not in err:
                raise AssertionError("S5 restore failure: the line about the failed restore was not printed: %r" % err)
            self.assertEqual((code, len(err.strip().splitlines())), (1, 1), err)
            self.assertIn("by hand", err)
            self.assertEqual(self.audit_text().count("issue_line.restore_failed"), 1)
            self.assertNotIn("issue_line.restored", self.audit_text())
            self.assertNotIn("wait\"", self.audit_text())

        def test_s14_a_restore_write_that_fails_prints_one_line_and_audits_restore_failed(self):
            self.check_restore_failed()

        def test_s14_a_mutant_that_swallows_the_failed_restore_is_caught_and_the_control_passes(self):
            old = "            except sb.IntentError:\n                sb.engine.audit.append(sb.engine.actor, \"issue_line.restore_failed\""
            new = "            except sb.IntentError:\n                pass\n            if False:\n                sb.engine.audit.append(sb.engine.actor, \"issue_line.restore_failed\""
            text = self.source_mutant(old, new, self.check_restore_failed)
            self.assertRegex(text, r"^S5 restore failure: the line about the failed restore was not printed")

        # -- S12: the Intent section ends at the first of the next heading and the Done-when line

        def source_mutant(self, old, new, check):
            """The control first (an unmutated SOURCE COPY of bin/issue_line.py passes `check`), then the copy with `old` replaced by `new`
            (asserted applied exactly once): `check` must fail, and its failure text is the check's own."""
            source = (REPO_ROOT / "bin" / "issue_line.py").read_text()
            self.assertEqual(source.count(old), 1, "the mutant is not applied exactly once")
            real = sb.load_module

            def copy_of(text):
                module = type(self.module)("issue_line_copy")
                exec(compile(text, "issue_line_copy.py", "exec"), module.__dict__)
                return lambda name: module if name == "issue_line" else real(name)
            with mock.patch.object(sb, "load_module", copy_of(source)):
                check()
            self.fresh()
            self.hook = None
            mutated = source.replace(old, new)
            self.assertNotEqual(mutated, source)
            with mock.patch.object(sb, "load_module", copy_of(mutated)):
                with self.assertRaises(AssertionError) as caught:
                    check()
            return str(caught.exception)

        BODY_A = "Why.\n\n## Intent\nFix it.\n\nDone when: ok.\nNext step (1 Nov): old\n"
        BODY_B = "Why.\n\n## Intent\nNext: x\nDone when: ok.\n"

        def next_lines(self, body):
            return [line for line in body.splitlines() if re.match(r"(Next step(\s*\([^)]*\))?|Next)\s*:", line)]

        def check_a(self):
            self.gh.bodies = {5: self.BODY_A}
            code, _, err = self.run_it(self.ref, "--next-step", "new")
            written = self.writes()[0]["body"]
            kind = self.next_lines(written)
            if len(kind) != 1:
                raise AssertionError("S12 found %d lines of the kind Next where 1 was expected" % len(kind))
            self.assertEqual((code, kind), (0, ["Next: new"]), err)

        def test_s12_a_next_line_after_the_done_when_line_is_replaced_not_duplicated(self):
            self.check_a()

        def test_s12_a_next_line_inside_the_intent_text_stays_and_the_new_line_is_appended(self):
            self.gh.bodies = {5: self.BODY_B}
            code, _, err = self.run_it(self.ref, "--next-step", "new")
            self.assertEqual((code, self.writes()[0]["body"]), (0, self.BODY_B + "\nNext: new\n"), err)

        def test_s12_a_mutant_whose_intent_section_runs_to_the_end_is_caught_and_the_control_passes(self):
            text = self.source_mutant(" or DONE_RE.match(lines[m])", "", self.check_a)
            self.assertRegex(text, r"^S12 found 2 lines of the kind Next where 1 was expected")

        # -- S13: no read-back could read the body

        def check_unverified(self):
            def down_after_the_write(count):
                if count == 3:
                    self.gh.down = True
            self.hook = down_after_the_write
            code, out, err = self.run_it(self.ref, "--next-step", "wait")
            if code != 1:
                raise AssertionError("S13 exited %d where 1 was expected (no read-back could read the body)" % code)
            self.assertEqual(len(self.writes()), 1)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertIn("written but could not be read back", err)
            self.assertNotIn("last good copy", err + out)
            self.assertNotIn("line written", out)
            self.assertEqual(self.audit_text().count("issue_line.unverified"), 1)
            self.assertNotIn("issue_line.restored", self.audit_text())
            self.assertNotIn("wait\"", self.audit_text())

        def test_s13_three_failed_read_backs_are_unverified_with_one_write_and_no_restore(self):
            self.check_unverified()

        def test_s13_a_mutant_that_exits_0_when_no_read_back_succeeded_is_caught_and_the_control_passes(self):
            text = self.source_mutant("return sb.EXIT_ERROR\n    if not saved", "return sb.EXIT_OK\n    if not saved", self.check_unverified)
            self.assertRegex(text, r"^S13 exited 0 where 1 was expected")

        # -- S6: bad arguments, before any read

        def test_s6_five_bad_arguments_are_refused_with_no_read(self):
            past = time.strftime("%Y-%m-%d", time.gmtime(self.world.clock.now() - 86400))
            far = time.strftime("%Y-%m-%d", time.gmtime(self.world.clock.now() + 400 * 86400))
            for argv in (("--next-step", ""), ("--next-step", "two\nlines"), ("--next-step", "x" * 601), ("--parked", past), ("--parked", far)):
                with self.subTest(argv[0]):
                    self.refused(self.ref, *argv)
                    self.assertEqual(self.reads, [])
                    self.assertEqual(sum(1 for c in self.gh.calls if len(c) == 3), 0)

        # -- U1, U2

        def test_u1_the_module_touches_no_file_and_no_github_and_the_command_has_one_writer(self):
            text = (REPO_ROOT / "bin" / "issue_line.py").read_text()
            code = re.sub(r'"""[\s\S]*?"""', "", text)
            for word in (r"(?<![.\w])open\(", r"\bimport (os|subprocess|socket|pathlib)", r"\bsubprocess\b", r"\burllib\b", r"\bPath\("):
                self.assertIsNone(re.search(word, code), word)
            entry = (REPO_ROOT / "bin" / "fleet-switchboard").read_text()
            start = entry.index("def cmd_issue_line")
            block = entry[start:entry.index("def add_issue_line_parsers")]
            self.assertEqual(block.count("write=write_issue_body"), 1)
            self.assertEqual(len(re.findall(r"\bPATCH\b|\.runner\(|subprocess", block)), 0)
            self.assertEqual(len(re.findall(r"sb\.write\(", code)), 2)   # the edit and the one restore, both through the handed-in write_issue_body

        def sbns(self, state):
            return type("NS", (), dict(fs=os, state=state, now=staticmethod(lambda: 1700000000.0), parse_bounded=sb.load_module("record_json").parse_bounded))()

        def test_u2_the_copy_has_three_keys_mode_0600_in_0700_through_a_temp_file_and_a_bounded_parse(self):
            self.run_it(self.ref, "--next-step", "wait")
            (path,) = self.copies()
            self.assertEqual(set(json.loads(path.read_text())), {"intent", "done_when", "at"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual([p for p in path.parent.iterdir() if p.suffix != ".json"], [])
            source = (REPO_ROOT / "bin" / "issue_line.py").read_text()
            self.assertIn("fs.replace(tmp, path)", source)
            self.assertIn("sb.parse_bounded(", source)
            self.assertNotIn("json.loads", source)

        def test_u2_the_copy_is_evicted_beyond_2000_files_oldest_first(self):
            state = Path(self.enterContext(host.tempfile.TemporaryDirectory()))
            directory = state / "issue-line"
            directory.mkdir(mode=0o700)
            for n in range(2000):
                path = directory / ("old%04d.json" % n)
                path.write_text("{}")
                os.utime(str(path), (1000 + n, 1000 + n))
            self.assertTrue(self.module.write_copy(self.sbns(state), "acme/api", 2))
            names = {p.name for p in directory.iterdir()}
            self.assertEqual(len(names), 2000)
            self.assertNotIn("old0000.json", names)
            self.assertIn("old0001.json", names)

        # -- S11: the docs and the two skills name the three statements

        STATEMENTS = (r"is the way to change one line of an issue body", r"whole-body write must keep the .{0,30}Intent.{0,60}Done when:.{0,40}gh shim refuses",
                      r"never changes the Intent or the Done-when")

        def test_s11_the_docs_section_and_the_two_skills_name_the_three_statements(self):
            docs = (REPO_ROOT / "docs" / "switchboard.md").read_text()
            section = docs[docs.index("## `issue-line`"):]
            places = [("docs", section)] + [(name, (REPO_ROOT / "skills" / name / "SKILL.md").read_text()) for name in ("fleet-charter", "fleet-coordination")]
            for name, text in places:
                for statement in self.STATEMENTS:
                    with self.subTest(place=name, statement=statement):
                        self.assertRegex(" ".join(text.split()), statement)
                self.assertIn("fleet-switchboard issue-line", text)

    return TestIssueLine
