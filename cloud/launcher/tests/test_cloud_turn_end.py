"""Ask #1484: a cloud worker that ended its turn and waits tells its recorded boss, once per turn end. Offline: a fake activity source, a fake send,
a scratch state directory. Mutants are the module's text changed in memory, asserted applied."""
import json, os, shutil, stat, sys, tempfile, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
import record_json  # noqa: E402
import cloud_end_alert as real  # noqa: E402

PARSE = record_json.parse_bounded
SOURCE = os.path.join(ROOT, "bin", "cloud_end_alert.py")
DOC = os.path.join(ROOT, "docs", "cloud-worker-interface.md")
WORDS = "ended its turn and waits for an instruction"
OWN_TEXT = "SECRET-ANSWER-LINE"


def mutant(*pairs):
    with open(SOURCE) as f:
        text = f.read()
    for old, new in pairs:
        assert text.count(old) == 1, "the mutation did not apply: %r" % old
        text = text.replace(old, new)
    module = types.ModuleType("cloud_end_alert_turn_mutant")
    exec(compile(text, "mutant", "exec"), module.__dict__)
    return module


class World(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="turn-end-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = os.path.join(self.dir, "cloud-end-alert.json")
        self.audit = os.path.join(self.dir, "audit.jsonl")
        self.sent, self.now, self.memory = [], 1000.0, {}
        self.acts = {}

    def send(self, to, text):
        self.sent.append((to, text))

    def run_pass(self, module=real, boss="coder-lead", worker="w1", advance=30):
        self.now += advance
        return module.scan_turns([(worker, boss)], lambda w: self.acts.get(w), self.state, self.audit, self.send, PARSE, self.now, self.memory)

    def turn_end(self, module=real, updated=500, boss="coder-lead"):
        """busy, then idle, then a pass past the 60 s hold."""
        self.acts["w1"] = (True, 0)
        self.run_pass(module, boss)
        self.acts["w1"] = (False, updated)
        self.run_pass(module, boss)
        self.run_pass(module, boss, advance=61)


class TestTurnEnd(World):
    def test_s2_a_turn_that_ends_sends_one_message_to_the_boss_with_the_facts_and_none_of_its_text(self):
        self.turn_end()
        self.assertEqual(len(self.sent), 1)
        to, text = self.sent[0]
        self.assertEqual(to, "coder-lead")
        for fact in ("w1", WORDS, "1970-01-01T00:08:20Z", "read w1"):
            self.assertIn(fact, text)
        self.assertNotIn(OWN_TEXT, text)

    def test_s3_five_passes_over_the_same_idle_state_send_one(self):
        self.turn_end()
        for _ in range(5):
            self.run_pass()
        self.assertEqual(len(self.sent), 1)

    def test_s3_a_busy_worker_sends_nothing(self):
        self.acts["w1"] = (True, 0)
        for _ in range(5):
            self.run_pass(advance=61)
        self.assertEqual(self.sent, [])

    def test_s3_a_second_turn_that_ends_sends_a_second(self):
        self.turn_end(updated=500)
        self.turn_end(updated=900)
        self.assertEqual(len(self.sent), 2)

    def test_s3_a_turn_that_ends_and_a_new_one_within_sixty_seconds_sends_nothing(self):
        self.acts["w1"] = (True, 0)
        self.run_pass()
        self.acts["w1"] = (False, 500)
        self.run_pass()
        self.acts["w1"] = (True, 0)
        self.run_pass(advance=30)
        self.run_pass(advance=61)
        self.assertEqual(self.sent, [])

    def test_s4_a_missing_or_invalid_boss_sends_to_the_chief_of_staff_alone(self):
        for boss in (None, "Bad Name!"):
            self.sent.clear()
            self.memory.clear()
            self.turn_end(boss=boss, updated=700 + len(str(boss)))
            self.assertEqual([to for to, _ in self.sent], ["chief-of-staff"])

    def test_s4_it_shares_the_state_file_keys_and_bound_of_the_alert(self):
        self.turn_end()
        data = json.load(open(self.state))
        self.assertEqual(data["sent"], [["w1@500", "turn_end"]])
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o600)
        with open(SOURCE) as f:
            text = f.read()
        self.assertEqual(text.count("def message_text("), 1)
        self.assertEqual(text.count("def save_state("), 1)
        self.assertEqual(text.count("[-SENT_KEEP:]"), 1)   # one bound, in the one save function
        self.assertEqual(text.count("def turn_message_text("), 1)
        self.assertEqual(text.count("send(to, text)"), 2)   # the alert's own call and the turn pass's: no other sender function

    def test_an_unreadable_activity_sends_nothing(self):
        self.run_pass()
        self.assertEqual(self.sent, [])


class TestMutants(World):
    def test_s5_a_mutant_that_sends_every_pass_is_not_one_message(self):
        broken = mutant(('ended = mem["busy"] or (mem["seen"] is not None and updated > mem["seen"])', "ended = True"),
                        ("            if key in tuple(keys):\n", "            if False:\n"))
        self.acts["w1"] = (False, 500)
        for _ in range(5):
            self.run_pass(broken, advance=61)
        self.assertGreater(len(self.sent), 1)

    def test_s5_a_mutant_that_sends_nothing_sends_no_message(self):
        broken = mutant(("                send(to, text)\n", "                pass\n"))
        self.turn_end(broken)
        self.assertEqual(self.sent, [])


class TestDocs(unittest.TestCase):
    def test_s6_the_document_says_what_is_sent_to_whom_and_when(self):
        with open(DOC) as f:
            text = f.read()
        for phrase in ("recorded boss", "once for each turn", "within two minutes", WORDS):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
