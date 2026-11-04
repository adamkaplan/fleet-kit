"""The every-kind delayed-mark tests of #1564, moved out of bin/test-switchboard (its line ceiling)."""
import unittest
from support.loader import sb
from support.wire_fakes import EPOCH


class TestEveryKindDelayedMark(unittest.TestCase):
    """#1564: the one age rule of #1402 marks every delivered kind older than 30 minutes, once; a kind with its own mark keeps only that."""

    NOW = EPOCH + 100000
    OWN = ("reminder",)   # a plain reminder says "late by N min" itself

    def text(self, kind, age, **extra):
        fact = sb.Fact("%s:k" % kind, "platform", kind, "a thing", self.NOW - age, batch=False, sender="coder", **extra)
        return sb.compose("platform", [fact], delivered=self.NOW)[0]

    def test_every_kind_in_the_table_carries_the_one_mark_or_its_own(self):
        kinds = sorted(set(sb.DESCRIBED_AS) | {"notice"})
        marked = [k for k in kinds if k not in self.OWN]
        self.assertGreaterEqual(len(marked), 6)
        for kind in kinds:
            first = self.text(kind, 2700).splitlines()[0]
            self.assertEqual(first.count("delayed: sent"), 0 if kind in self.OWN else 1, kind)
            if kind not in self.OWN:
                self.assertIn("delayed: sent %s" % sb.clock_utc(self.NOW - 2700), first, kind)

    def test_a_fresh_message_of_every_kind_has_no_mark(self):
        for kind in sorted(set(sb.DESCRIBED_AS) | {"notice", "reminder"}):
            for age in (0, 600, 1800):
                self.assertNotIn("delayed:", self.text(kind, age), (kind, age))

    def test_a_late_peer_send_is_marked_and_a_reminder_keeps_its_own_note_only(self):
        self.assertIn("delayed: sent", self.text("send", 2700).splitlines()[0])
        reminder = sb.Fact("reminder:r", "platform", "reminder", "ping (late by 45 min)", self.NOW - 2700, batch=False, sender="coder")
        text = sb.compose("platform", [reminder], delivered=self.NOW)[0]
        self.assertEqual(text.splitlines()[-1].count("late by"), 1)   # the item line (the header repeats the lead text)
        self.assertEqual(text.count("delayed:"), 0)
        alert = sb.Fact("alert:a", "platform", "alert", "x", self.NOW - 2700, batch=False)
        self.assertEqual(sb.compose("platform", [alert], delivered=self.NOW)[0].count("delayed: sent"), 1)

    def test_a_retried_send_shows_the_time_the_sender_sent_it(self):
        fact = sb.Fact("send:coder:1", "platform", "send", "hello", self.NOW - 7200 - 600, batch=False, sender="coder")
        text = sb.compose("platform", [fact], delivered=self.NOW)[0]   # delivered two hours after the first try
        self.assertEqual(text.count("delayed: sent %s" % sb.clock_utc(fact.created)), 1)
        self.assertNotIn("delayed: sent %s" % sb.clock_utc(self.NOW - 600), text)

    def test_an_old_order_is_marked_in_its_first_line_and_a_fresh_one_is_not(self):
        order = lambda age: sb.Fact("orders:1", "platform", "orders", "- keep going", self.NOW - age, repo="acme/api", issue=3, orders=True)
        late = sb.compose("platform", [order(2700)], delivered=self.NOW)[0]
        self.assertIn("delayed: sent", late.splitlines()[0])
        self.assertEqual(late.count("delayed:"), 1)
        self.assertNotIn("delayed:", sb.compose("platform", [order(60)], delivered=self.NOW)[0])
