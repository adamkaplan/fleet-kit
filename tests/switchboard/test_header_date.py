"""The header date and held-time tests of #1800 (S2 to S6, S8, U1)."""
import inspect
import time
import unittest
from support.loader import sb
from support.wire_fakes import EPOCH

MID = EPOCH - 8 * 3600   # 2027-01-15 00:00Z
NOW = EPOCH + 100000   # 2027-01-16 11:46:40Z
BASELINE = [   # the first header lines the code printed before #1800, for the 20 fixtures of baseline_headers()
    '[switchboard] 1 for platform (sent 11:46Z, delivered 11:46Z): msg 0',
    '[switchboard] 2 for platform (sent 11:45Z, delivered 11:46Z): msg 0 [newest first]',
    '[switchboard] 3 for platform (sent 11:43Z, delivered 11:46Z): msg 0 [newest first]',
    '[switchboard] 1 for platform (sent 11:42Z, delivered 11:46Z): msg 0',
    '[switchboard] 2 for platform (sent 11:40Z, delivered 11:46Z): msg 0 [newest first]',
    '[switchboard] 3 for platform (sent 11:38Z-11:39Z, delivered 11:46Z): msg 0 [newest first]',
    '[switchboard] 1 for platform (sent 11:37Z, delivered 11:46Z): msg 0',
    '[switchboard] 2 for platform (sent 11:36Z, delivered 11:46Z): msg 0 [newest first]',
    '[switchboard] 3 for platform (sent 11:34Z, delivered 11:46Z): msg 0 [newest first]',
    '[switchboard] 1 for platform (sent 11:33Z, delivered 11:46Z): msg 0',
    '[switchboard] 2 for platform (sent 11:31Z, delivered 11:46Z): msg 0 [newest first]',
    '[switchboard] 3 for platform (sent 11:29Z-11:30Z, delivered 11:46Z): msg 0 [newest first]',
    '[switchboard] 1 for platform (sent 11:28Z, delivered 11:46Z): msg 0',
    '[switchboard] 2 for platform (sent 11:27Z, delivered 11:46Z): msg 0 [newest first]',
    '[switchboard] 3 for platform (sent 11:25Z, delivered 11:46Z): msg 0 [newest first]',
    '[switchboard] 1 for platform (sent 11:24Z, delivered 11:46Z): msg 0',
    '[switchboard] 2 for platform (sent 11:22Z, delivered 11:46Z): msg 0 [newest first]',
    '[switchboard] 3 for platform (sent 11:20Z-11:21Z, delivered 11:46Z): msg 0 [newest first]',
    '[switchboard] 1 for platform (sent 11:19Z, delivered 11:46Z): msg 0',
    '[switchboard] 2 for platform (sent 11:18Z, delivered 11:46Z): msg 0 [newest first]',
]


def header_fact(key, age, sender="coder"):
    return sb.Fact(key, "platform", "send", "msg", NOW - age, batch=False, sender=sender)


def baseline_headers():
    out = []
    for i in range(20):
        ages = [i * 90, (i * 90) % 1800]
        facts = [sb.Fact("send:c:%d" % j, "platform", "send", "msg %d" % j, NOW - (ages[0] if j == 0 else ages[1] + j * 7), batch=False, sender="coder")
                 for j in range(1 + i % 3)]
        out.append(sb.compose("platform", facts, delivered=NOW)[0].splitlines()[0])
    return out


def previous_day_check(clock_day):
    """The S2 date check, on any clock_day function."""
    sent = MID + 15 * 3600 + 15 * 60   # 2027-01-15 15:15Z
    delivered = MID + 86400 + 14 * 3600 + 23 * 60   # 2027-01-16 14:23Z
    text = clock_day(sent, delivered)
    assert "2027-01-15 15:15Z" in text, "the sent time of the previous day carries its date"
    return text


class TestHeaderDate(unittest.TestCase):
    def header(self, facts, delivered=NOW):
        return sb.compose("platform", facts, delivered=delivered)[0].splitlines()[0]

    def test_s2_a_member_of_the_previous_day_carries_its_date(self):
        delivered = MID + 86400 + 14 * 3600 + 23 * 60   # 2027-01-16 14:23Z
        created = MID + 15 * 3600 + 15 * 60             # 2027-01-15 15:15Z
        head = self.header([sb.Fact("send:c:1", "platform", "send", "msg", created, batch=False, sender="coder")], delivered)
        self.assertIn("2027-01-15 15:15Z", head)
        self.assertIn("delivered 14:23Z", head)
        self.assertNotIn("sent 15:15Z", head)
        self.assertEqual(previous_day_check(sb.clock_day), "2027-01-15 15:15Z")

    def test_s3_a_member_held_5_hours_or_40_minutes_says_how_long(self):
        self.assertIn("held 5 h", self.header([header_fact("send:c:1", 5 * 3600 + 120)]))
        self.assertIn("held 40 min", self.header([header_fact("send:c:2", 40 * 60 + 5)]))
        self.assertIn("held 119 min", self.header([header_fact("send:c:3", 119 * 60 + 5)]))

    def test_s4_twenty_same_day_headers_equal_the_stored_baseline(self):
        self.assertEqual(len(BASELINE), 20)
        self.assertEqual(sum(1 for a, b in zip(baseline_headers(), BASELINE) if a != b), 0)

    def test_s5_a_merged_header_of_two_days_dates_the_oldest_once(self):
        old = MID + 20 * 3600     # 2027-01-15 20:00Z
        facts = [sb.Fact("send:c:1", "platform", "send", "old", old, batch=False, sender="a"),
                 sb.Fact("send:d:1", "platform", "send", "new", NOW - 60, batch=False, sender="b")]
        head = self.header(facts)
        self.assertEqual(head.count("2027-01-15 20:00Z"), 1)
        self.assertEqual(head.count("2027-01-16"), 0)   # the newest is on the delivery's day: no date

    def test_s6_a_mutant_without_the_date_fails_the_date_check(self):
        source = inspect.getsource(sb.clock_day)
        mutant_source = source.replace('return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(epoch))', "return clock_utc(epoch)")
        self.assertNotEqual(mutant_source, source, "the mutation is applied")
        mutant, control = ({"time": time, "clock_utc": sb.clock_utc} for _ in range(2))
        exec(mutant_source, mutant)
        exec(source, control)
        previous_day_check(control["clock_day"])   # control first: it passes
        with self.assertRaisesRegex(AssertionError, "^the sent time of the previous day carries its date"):
            previous_day_check(mutant["clock_day"])

    def test_s8_a_time_that_is_not_a_time_prints_question_marks_and_is_delivered(self):
        text, carried = sb.compose("platform", [sb.Fact("send:c:1", "platform", "send", "msg", 1e30, batch=False, sender="coder")], delivered=NOW)
        self.assertIn("??:??Z", text.splitlines()[0])
        self.assertEqual(len(carried), 1)

    def test_u1_without_a_delivery_time_the_text_has_no_header_time(self):
        self.assertNotIn("sent", sb.compose("platform", [header_fact("send:c:1", 0)])[0].splitlines()[0])


def mutant_of(function, old, new):
    """(control, mutant) copies of a function of decision.py: the mutation is one textual replacement, asserted applied."""
    source = inspect.getsource(function)
    mutated = source.replace(old, new)
    assert mutated != source and source.count(old) == 1, "the mutation is applied once: " + old
    made = []
    for text in (source, mutated):
        world = dict(vars(sb))
        exec(text, world)
        made.append(world[function.__name__])
    return made


class TestHeaderDateCallSites(unittest.TestCase):
    D = MID + 86400 + 14 * 3600 + 23 * 60   # 2027-01-16 14:23Z
    PREV = MID + 15 * 3600 + 15 * 60        # 2027-01-15 15:15Z

    def head(self, compose, facts, delivered=None):
        return compose("platform", facts, delivered=self.D if delivered is None else delivered)[0].splitlines()[0]

    def check_a(self, compose):
        facts = [sb.Fact("reminder:1", "platform", "reminder", "r", self.PREV, batch=False),
                 sb.Fact("reminder:2", "platform", "reminder", "r", self.D - 600, batch=False)]
        head = self.head(compose, facts)
        assert "sent 2027-01-15 15:15Z-14:13Z, delivered 14:23Z" in head, "a non-late range dates its oldest member: " + head

    def check_b(self, compose):
        fact = sb.Fact("send:c:1", "platform", "send", "msg", self.D - 60, batch=False, sender="coder")
        fact.supersedes = (self.PREV,)
        head = self.head(compose, [fact])
        assert "still waiting: sent 2027-01-15 15:15Z)" in head, "the replaced earlier message carries its date: " + head

    def check_c(self, compose):
        fact = sb.Fact("orders:1:ab:start", "platform", "orders", "approve", self.PREV, issue=1, orders=True)
        head = self.head(compose, [fact])
        assert "delayed: sent 2027-01-15 15:15Z, delivered 14:23Z" in head, "the single late note carries the date: " + head

    def run_mutant(self, check, old, new, message):
        control, mutant = mutant_of(sb.compose, old, new)
        check(control)   # control first: it passes
        with self.assertRaisesRegex(AssertionError, "^" + message):
            check(mutant)
        return control

    def test_m1a_the_non_late_range_dates_its_oldest_member(self):
        self.run_mutant(self.check_a, "clock_day(min(sent_at(f) for f in taken), delivered)", "clock_utc(min(sent_at(f) for f in taken))",
                        "a non-late range dates its oldest member")

    def test_m1b_the_replaced_earlier_messages_carry_their_date(self):
        self.run_mutant(self.check_b, "clock_day(t, delivered) for t in waiting[:5]", "clock_utc(t) for t in waiting[:5]",
                        "the replaced earlier message carries its date")

    def test_m1c_the_single_message_late_note_carries_the_date(self):
        self.run_mutant(self.check_c, "(first.rstrip(\":\"), clock_day(late, delivered)", "(first.rstrip(\":\"), clock_utc(late)",
                        "the single late note carries the date")

    def test_m2_without_a_delivery_time_the_plain_clock_text_is_returned(self):
        def check(clock_day):
            assert clock_day(self.PREV, None) == sb.clock_utc(self.PREV) == "15:15Z", "no delivery time: no date"
        control, mutant = mutant_of(sb.clock_day, "    if delivered is None:\n        return clock_utc(epoch)\n", "")
        check(control)
        with self.assertRaisesRegex(AssertionError, "^no delivery time: no date"):
            check(mutant)

    def test_l5_boundaries(self):
        fact = lambda age: sb.Fact("send:c:1", "platform", "send", "msg", NOW - age, batch=False, sender="coder")
        self.assertIn("held 2 h", self.head(sb.compose, [fact(7200)], NOW))
        self.assertIn("held 119 min", self.head(sb.compose, [fact(7199)], NOW))
        day = MID + 86400   # 2027-01-16 00:00:00Z
        self.assertEqual(sb.clock_day(day - 1, day), "2027-01-15 23:59Z")
        self.assertEqual(sb.clock_day(day, day), "00:00Z")
        control, mutant = mutant_of(sb.clock_day, "time.gmtime(delivered)[:3]", "time.gmtime(delivered + 1)[:3]")
        late = day + 86399   # 23:59:59Z: a delivery one second before midnight; sent 12:00Z the same day prints no date
        self.assertEqual(control(day + 43200, late), "12:00Z")
        self.assertEqual(mutant(day + 43200, late), "2027-01-16 12:00Z")   # the mutant (delivered+1) crosses midnight: the check above fails on it
        self.assertEqual(control(day - 1, day), "2027-01-15 23:59Z")
        control, mutant = mutant_of(sb.compose, "held >= 7200", "held > 7200")
        self.assertIn("held 2 h", self.head(control, [fact(7200)], NOW))
        self.assertIn("held 120 min", self.head(mutant, [fact(7200)], NOW))
