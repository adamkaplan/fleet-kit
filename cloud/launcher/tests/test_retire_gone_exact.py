"""A retire of a gone cloud worker is audited, exact and bound to the pod it records (ask #1327, scenarios S1 to S7, U1).

The cluster is the FAKE one of the launcher tests (fake_kubectl); a not-found text is served by a tiny stand-in script. Every
mutant is a patch of the running code, and each test asserts that the mutation was applied.
"""
import contextlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as tcw  # noqa: E402
import test_retire_gone_pod as trg  # noqa: E402

cw, sbcw = tcw.cw, trg.sbcw
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
REGISTRY = "cloud-hub-registry"
OTHER = {"other-agent": "f" * 64}
SWITCHBOARD_NAME_RE = r"^[a-z][a-z0-9_-]{0,31}$"   # the switchboard's agent name rule (a test below reads it from the switchboard)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


class GoneBase(tcw.Base):
    def gone_worker(self, *extra):
        """A created worker whose pod an operator deleted: both Secrets are left."""
        self.create("coder7", *extra)
        trg.plant_run_secret(self)
        st = self.st()
        st["pods"].pop(cw.pod_name("coder7"))
        self.write_state(st)

    def stop(self, *extra):
        return self.run_cli("stop", "coder7", "--reason", "retire", *extra)


class AuditNamesSecrets(GoneBase):
    def test_s2_the_stop_audit_names_the_secrets_removed_and_no_content(self):
        self.gone_worker()
        code, out, err = self.stop()
        self.assertEqual(code, 0, err)
        line = [x for x in self.audit_lines() if x["action"] == "stop"][-1]
        pw = cw.pod_name("coder7")
        print("%d of 2 Secret names in the audit reason" % len([n for n in (pw + "-pw", pw + "-run") if n in line["reason"]]))
        self.assertIn(pw + "-pw", line["reason"])
        self.assertIn(pw + "-run", line["reason"])
        self.assertIsNone(re.search(r"gh[pousr]_|github_pat_|sk-[A-Za-z0-9]{20}|eyJ", json.dumps(line)))

    def test_s2_a_stop_that_removed_nothing_says_so(self):
        self.gone_worker()
        self.assertEqual(self.stop()[0], 0)
        trg.plant_run_secret(self)
        st = self.st()
        st["secrets"] = {}
        self.write_state(st)
        self.assertEqual(self.stop()[0], 0)
        self.assertIn("secrets removed: none", [x for x in self.audit_lines() if x["action"] == "stop"][-1]["reason"])


class NotFoundExact(tcw.Base):
    """S3: the match is the whole message of the one object asked for (kind and exact name), one trailing newline trimmed."""
    NAME = "cw-coder7"

    def kube_saying(self, text):
        script = os.path.join(self.tmp.name, "says-%d" % len(os.listdir(self.tmp.name)))
        with open(script, "w") as handle:
            handle.write("#!/bin/sh\ncat <<'EOF' >&2\n%s\nEOF\nexit 1\n" % text)   # one newline after the text, as a real client prints
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
        return cw.Kube(dict(self.env, CW_KUBECTL=script))

    def exact(self, kind):
        return 'Error from server (NotFound): %s "%s" not found' % (kind, self.NAME)

    def cases(self, kind, other):
        e = self.exact(kind)
        return {"another object": 'Error from server (NotFound): %s "someone-else" not found' % kind,
                "text after the words": e + ", try again later",
                "the words in another sentence": "note: NotFound is meant by the docs; " + e,
                "another kind": 'Error from server (NotFound): %s "%s" not found' % (other, self.NAME)}

    def table(self, label, read_it, kind, other):
        rows = []
        for case, text in self.cases(kind, other).items():
            try:
                rows.append((case, "read as gone" if read_it(self.kube_saying(text)) is None else "answered"))
            except cw.CloudError:
                rows.append((case, "raises"))
        for case, got in rows:
            print("%s | %s | %s" % (label, case, got))
        self.assertEqual([got for _, got in rows], ["raises"] * 4, rows)
        self.assertIsNone(read_it(self.kube_saying(self.exact(kind))), "the exact message reads as gone")

    def test_s3_pod_read_near_misses_raise_and_the_exact_message_is_gone(self):
        self.table("pod read", lambda k: k.get_pod(self.NAME), "pods", "secrets")

    def test_s3_secret_read_near_misses_raise_and_the_exact_message_is_gone(self):
        self.table("secret read", lambda k: k.get_secret(self.NAME), "secrets", "pods")

    def test_s3_the_per_run_secret_read_uses_the_same_match(self):
        kube = self.kube_saying('Error from server (NotFound): secrets "cw-coder7-run" not found, and more')
        with self.assertRaises(cw.CloudError):
            kube.get_run_secret("cw-coder7")
        self.assertIsNone(self.kube_saying('Error from server (NotFound): secrets "cw-coder7-run" not found').get_run_secret("cw-coder7"))

    def test_s3_one_trailing_newline_is_trimmed_and_only_one(self):
        self.assertIsNone(self.kube_saying(self.exact("pods")).get_pod(self.NAME))
        with self.assertRaises(cw.CloudError):
            self.kube_saying(self.exact("pods") + "\n").get_pod(self.NAME)   # two newlines in all


class NameRule(GoneBase):
    def test_s4_a_name_with_a_trailing_newline_is_refused_and_valid_names_are_not(self):
        code, out, err = self.run_cli("stop", "coder7\n", "--reason", "x")
        self.assertEqual(code, 2, err)
        self.assertFalse(os.path.exists(self.env["FAKE_KUBE_CALLS"]), "a cluster call was made for a name with a newline")
        refused = [n for n in ("coder7", "a", "worker-1", "orch_1", "a" * 32) if self.run_cli("stop", n, "--reason", "x")[0] == 2]
        print("1 of 1 newline name refused and %d of 5 valid names refused" % len(refused))
        self.assertEqual(refused, [])

    def test_s4_the_launcher_has_no_loose_name_match_left(self):
        self.assertNotRegex(read("cloud/launcher/cloud_worker.py"), r"(?<![A-Za-z_])NAME_RE\.match\(")
        self.assertNotRegex(read("cloud/launcher/run_task.py"), r"NAME_RE\.match\(")

    def test_s4_the_switchboard_side_refuses_it_and_a_name_with_a_newline_reaches_no_path(self):
        self.assertIn(SWITCHBOARD_NAME_RE, read("bin/fleet-switchboard"))
        with mock.patch.dict(sbcw.H, {"NAME_RE": re.compile(SWITCHBOARD_NAME_RE)}):
            self.assertFalse(sbcw.valid_name("coder7\n"))
            self.assertTrue(all(sbcw.valid_name(n) for n in ("coder7", "a", "worker-1", "orch_1", "a" * 32)))
            for path in (sbcw.record_path, sbcw.password_path):
                with self.assertRaises(sbcw.CloudError):
                    path("/state", "coder7\n")


class BoundToThePod(GoneBase):
    def hook(self):
        return mock.patch.object(cw.Launcher, "run_hook", lambda launcher, name: {"pushed": False, "wip_branch": None})

    def new_pod_under_the_name(self):
        """The old pod is replaced by another under the same name (another uid); both Secrets stay."""
        st = self.st()
        pod = st["pods"][cw.pod_name("coder7")]
        pod["metadata"]["uid"] = "uid-new-77"
        self.write_state(st)

    def left_whole(self):
        st = self.st()
        return (st["pods"].get(cw.pod_name("coder7"), {}).get("metadata", {}).get("uid") == "uid-new-77"
                and sorted(st["secrets"]) == [cw.pod_name("coder7") + "-pw", cw.pod_name("coder7") + "-run"])

    def raced_retire(self):
        made = self.create("coder7")
        trg.plant_run_secret(self)
        self.new_pod_under_the_name()
        with self.hook():
            code, out, err = self.stop("--pod-uid", made["pod_uid"])
        return code, err

    def test_s5_create_reports_the_uid_of_the_pod_it_made(self):
        made = self.create("coder7")
        self.assertEqual(made["pod_uid"], self.pod()["metadata"]["uid"])
        self.assertTrue(made["pod_uid"])

    def test_s5_a_new_pod_under_the_name_is_left_whole_with_its_secrets(self):
        code, err = self.raced_retire()
        self.assertEqual(code, 6, err)
        self.assertTrue(self.left_whole(), "0 objects of the new pod may be deleted")
        self.assertFalse([c for c in open(self.env["FAKE_KUBE_CALLS"]).read().splitlines() if '"delete"' in c], "a delete was sent")

    def test_s5_a_mutant_that_ignores_the_uid_fails_the_test(self):
        real, applied = cw.Launcher.stop, []

        def mutant(launcher, a, reason=None):
            applied.append(1)
            a.pod_uid = None   # the uid is ignored: the retire acts by name
            return real(launcher, a, reason)
        with mock.patch.object(cw.Launcher, "stop", mutant):
            code, err = self.raced_retire()
        self.assertTrue(applied, "the mutation was not applied")
        self.assertFalse(self.left_whole(), "the mutant was not killed: it left the new pod whole")

    def test_s5_a_pod_that_reads_as_gone_is_retired_by_name_only_when_no_live_pod_exists(self):
        self.gone_worker()
        real, reads = cw.Kube.get_pod, []

        def late(kube, name):   # the first read says gone; a new pod holds the name by the second
            reads.append(name)
            if len(reads) == 1:
                return None
            return {"metadata": {"name": name, "uid": "uid-new-5"}, "status": {"phase": "Running"}}
        with mock.patch.object(cw.Kube, "get_pod", late):
            code, out, err = self.stop()
        self.assertEqual(code, 6, err)
        self.assertEqual(len(self.st()["secrets"]), 2, "a Secret was deleted while a pod holds the name")

    def test_s5_a_record_with_no_uid_acts_as_before(self):
        self.gone_worker()
        self.assertEqual(self.stop()[0], 0)
        self.assertEqual(self.st()["secrets"], {})


class HubEntry(tcw.Base):
    def setUp(self):
        super().setUp()
        self.patch_state(secrets={REGISTRY: 1, "cloud-hub-pw": 1},
                         secret_bodies={"cloud-hub-pw": {"keep": "kept-pw"}, REGISTRY: {"secrets.json": json.dumps(OTHER), "agents.json": json.dumps({"other-agent": {"url": "u"}})}},
                         hook_result={"pushed": False, "wip_branch": None})

    def gone_orchestrator(self):
        code, out, err = self.run_cli("create", "orch1", "--repo", "acme-org/widget-service", "--branch", "main", "--agent", "orchestrator", "--boss", "boss1")
        self.assertEqual(code, 0, err)
        st = self.st()
        st["pods"].pop(cw.pod_name("orch1"))
        self.write_state(st)

    def agents(self):
        return json.loads(self.st()["secret_bodies"][REGISTRY]["agents.json"])

    def retire(self):
        return self.run_cli("stop", "orch1", "--reason", "retire", "--hub-entry")

    def test_s6_a_gone_orchestrator_loses_its_hub_entry(self):
        self.gone_orchestrator()
        self.assertIn("orch1", self.agents())
        code, out, err = self.retire()
        self.assertEqual(code, 0, err)
        print("%d of 1 hub entry removed" % (0 if "orch1" in self.agents() else 1))
        self.assertEqual(list(self.agents()), ["other-agent"])
        self.assertNotIn("orch1", json.loads(self.st()["secret_bodies"][REGISTRY]["secrets.json"]))

    def test_s6_a_mutant_that_skips_the_removal_leaves_the_entry(self):
        self.gone_orchestrator()
        applied = []
        with mock.patch.object(cw.Launcher, "_unregister", lambda launcher, agent: applied.append(agent) or True):
            self.assertEqual(self.retire()[0], 0)
        self.assertTrue(applied, "the mutation was not applied")
        self.assertIn("orch1", self.agents(), "the mutant left no entry: the test would not catch it")

    def test_s6_a_failure_of_the_removal_is_reported_not_swallowed(self):
        self.gone_orchestrator()
        real = cw.Kube.registry_update

        def broken(kube, secret, change):
            raise cw.CloudError("the hub registry Secret %s is not found" % secret)
        with mock.patch.object(cw.Kube, "registry_update", broken):
            code, out, err = self.retire()
        self.assertNotEqual(code, 0)
        self.assertIn("hub registry entry", err)
        self.assertIn("orch1", self.agents())
        self.assertFalse([x for x in self.audit_lines() if x["action"] == "stop"][-1]["ok"])

    def test_s6_a_gone_worker_that_is_not_a_hub_orchestrator_makes_no_registry_call(self):
        self.gone_orchestrator()
        self.assertEqual(self.run_cli("stop", "orch1", "--reason", "retire")[0], 0)
        self.assertIn("orch1", self.agents())


class RetireSide(unittest.TestCase):
    """The switchboard side: the record carries the uid and a hub orchestrator is retired with its entry."""
    setUp = trg.Retire.setUp
    touch = trg.Retire.touch

    def calls_of(self, record):
        seen = []

        def launcher(runner, verb, name, extra, seconds, cluster=None):
            seen.append(list(extra))
            return 0, {"runtime_id": "", "pushed": False, "pod_gone": True, "secrets_removed": []}, ""
        args = types.SimpleNamespace(name="coder7", reason="done", undo=False)
        with mock.patch.object(sbcw, "run_launcher", launcher), contextlib.redirect_stdout(io.StringIO()):
            sbcw.cmd_retire(args, None, None, dict({"runtime_id": "pod/coder7"}, **record))
        return seen[0]

    def test_s5_the_record_from_a_create_carries_the_uid(self):
        body = {"worker": "coder7", "url": "https://worker-cw-coder7.example.internal:4096", "runtime_id": "pod/cw-coder7", "pod_uid": "uid-1"}
        self.assertEqual(sbcw._record_from_create("coder7", body)["pod_uid"], "uid-1")
        del body["pod_uid"]
        self.assertNotIn("pod_uid", sbcw._record_from_create("coder7", body))

    def test_s5_the_retire_hands_the_recorded_uid_to_the_launcher(self):
        extra = self.calls_of({"pod_uid": "uid-1"})
        self.assertEqual(extra[extra.index("--pod-uid") + 1], "uid-1")
        self.assertNotIn("--pod-uid", self.calls_of({}))

    def test_s6_an_orchestrator_record_asks_for_its_hub_entry_to_go(self):
        self.assertIn("--hub-entry", self.calls_of({"agent": "orchestrator"}))
        self.assertNotIn("--hub-entry", self.calls_of({"agent": "coder"}))


class UnreadIdentity(tcw.Base):
    """S9: a create whose read of the new pod fails says so once on stderr and in the audit."""
    MARK = "pod identity"

    def create_with_failed_read(self, patches=(), answer=None, worker="coder7"):
        """answer: None raises at the read; else a function (real pod) -> what the read returns."""
        real_wait, real_get, state = cw.Launcher._wait_ready, cw.Kube.get_pod, {"ready": False}

        def wait(launcher, name, seconds):
            result = real_wait(launcher, name, seconds)
            state["ready"] = True   # every read from here on is the create's read of the uid
            return result

        def get(kube, name):
            if state["ready"]:
                if answer is None:
                    raise cw.CloudError("the cluster did not answer")
                return answer(real_get(kube, name))
            return real_get(kube, name)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(cw.Launcher, "_wait_ready", wait))
            stack.enter_context(mock.patch.object(cw.Kube, "get_pod", get))
            for p in patches:
                stack.enter_context(p)
            return self.run_cli("create", worker, "--repo", "acme-org/widget-service", "--branch", "main")

    def lines(self, err):
        return [x for x in err.splitlines() if self.MARK in x]

    def test_s9_a_failed_read_gives_one_stderr_line_naming_the_worker_and_one_audit_line(self):
        code, out, err = self.create_with_failed_read()
        self.assertEqual(code, 0, err)
        audited = [x for x in self.audit_lines() if self.MARK in x["reason"]]
        print("%d of 1 stderr line, %d of 1 audit line" % (len(self.lines(err)), len(audited)))
        self.assertEqual(len(self.lines(err)), 1, err)
        self.assertIn("coder7", self.lines(err)[0])
        self.assertEqual(len(audited), 1)
        self.assertEqual(json.loads(out)["pod_uid"], "")

    def test_s9_every_way_the_uid_is_missing_gives_the_same_one_line_and_one_audit_line(self):
        cases = {"the read raises": None, "the read returns nothing": lambda pod: None,
                 "the pod has no uid": lambda pod: {"metadata": {}},
                 "the uid is empty": lambda pod: {"metadata": {"uid": ""}},
                 "the uid is not text": lambda pod: {"metadata": {"uid": 7}}}
        template = "cloud-worker: WARNING: the pod identity of %s could not be read; the worker record holds none and a retire acts by name"
        for n, (label, answer) in enumerate(cases.items()):
            with self.subTest(label):
                worker = "coder%d" % n
                code, out, err = self.create_with_failed_read(answer=answer, worker=worker)
                self.assertEqual(code, 0, err)
                print("%s: %d of 1 stderr line" % (label, len(self.lines(err))))
                want = template % worker
                self.assertEqual(self.lines(err), [want])
                audited = [x for x in self.audit_lines() if self.MARK in x["reason"]]
                self.assertEqual(len(audited), n + 1)   # one line per create, none more
                self.assertIn(want.split("WARNING: ")[1], audited[-1]["reason"])
                self.assertEqual(json.loads(out)["pod_uid"], "")

    def test_s9_a_mutant_that_notes_only_the_raising_case_fails_the_other_cases(self):
        real, applied = cw.Launcher._read_pod_uid, []

        def only_raising(launcher, worker, name, info):
            applied.append(worker)
            try:
                uid = ((launcher.kube.get_pod(name) or {}).get("metadata") or {}).get("uid")
            except cw.CloudError:
                launcher._note_unread_identity(worker, info)
                return ""
            return uid or ""   # nothing is noted for a read that answers without a usable uid
        mutant = mock.patch.object(cw.Launcher, "_read_pod_uid", only_raising)
        code, out, err = self.create_with_failed_read([mutant])
        self.assertTrue(applied, "the mutation was not applied")
        self.assertEqual(len(self.lines(err)), 1, "the mutant still notes the raising case")
        applied.clear()
        code, out, err = self.create_with_failed_read([mutant], answer=lambda pod: None, worker="coder8")
        self.assertTrue(applied, "the mutation was not applied")
        print("mutant: %d stderr lines for a read that returns nothing (the check wants 1)" % len(self.lines(err)))
        self.assertEqual(self.lines(err), [], "the mutant is silent on a read that returns nothing")

    def test_s9_a_mutant_that_records_the_empty_identity_silently_fails_the_test(self):
        applied = []
        silent = mock.patch.object(cw.Launcher, "_note_unread_identity", lambda launcher, worker, info: applied.append(worker))
        code, out, err = self.create_with_failed_read([silent])
        self.assertTrue(applied, "the mutation was not applied")
        print("mutant: %d stderr lines (the test wants 1)" % len(self.lines(err)))
        self.assertEqual(len(self.lines(err)), 0)
        self.assertEqual([x for x in self.audit_lines() if self.MARK in x["reason"]], [])

    def test_s9_a_create_whose_read_works_prints_no_such_line(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cw.main(["create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main"], self.env, self.clock, lambda s: self.clock.advance(seconds=s))
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(self.lines(err.getvalue()), [])
        self.assertEqual([x for x in self.audit_lines() if self.MARK in x["reason"]], [])


class Documents(unittest.TestCase):
    CAUSES = ("stop audit did not name the Secrets it removed", "not-found match was a substring match", "Secret read still used the loose match",
              "name with a trailing newline passed the name pattern", "record was not bound to a pod identity", "hub entry of a gone orchestrator was left behind")
    SAYS = ("the stop audit names the Secrets it removed", "the not-found match is anchored to the whole message of the one object asked for",
            "a name with a trailing newline is refused", "the worker record carries the pod's uid and a retire acts only on the pod with that uid",
            "a retire of a gone orchestrator removes its hub entry and reports a failure to do so")

    def section(self, rel, title):
        text = read(rel)
        self.assertIn(title, text)
        return text.split(title, 1)[1].split("\n## ", 1)[0]

    def test_s1_the_readme_retire_section_names_the_six_causes(self):
        part = self.section("cloud/launcher/README.md", "## Retire of a gone worker (ask #1327)")
        found = [c for c in self.CAUSES if c.lower() in part.lower()]
        print("the section names %d of 6 causes" % len(found))
        self.assertEqual(found, list(self.CAUSES))

    def test_s7_both_documents_say_each_behaviour(self):
        for rel, title in (("cloud/launcher/README.md", "## Retire of a gone worker (ask #1327)"), ("docs/cloud-worker-interface.md", "## Retire of a gone worker (ask #1327)")):
            part = self.section(rel, title)
            found = [s for s in self.SAYS if s.lower() in part.lower()]
            print("%d of 5 statements in %s" % (len(found), rel))
            self.assertEqual(found, list(self.SAYS), rel)

    UNREAD = "a create whose read of the new pod fails cannot record the pod's uid: it says so in one stderr line and in its audit line"

    def test_s9_both_documents_say_a_create_that_cannot_read_the_uid_says_so(self):
        for rel, title in (("cloud/launcher/README.md", "## Retire of a gone worker (ask #1327)"), ("docs/cloud-worker-interface.md", "## Retire of a gone worker (ask #1327)")):
            self.assertIn(self.UNREAD, self.section(rel, title).lower(), rel)


if __name__ == "__main__":
    unittest.main()
