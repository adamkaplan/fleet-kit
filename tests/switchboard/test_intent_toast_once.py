"""(#1726) An Intent edit is toasted once per edit stamp, whatever other toast took the agent's slot between."""
import types
import unittest
from support.loader import sb


class FakeSource:
    def __init__(self, reasons):
        self.reasons = reasons

    def toasts(self):
        return [("cos", r, "Intent changed", "body") for r in self.reasons]


def world(source):
    engine = types.SimpleNamespace(sources=[source], pass_toasts={}, toasted={}, config={},
                                   herdr=types.SimpleNamespace(shown=[]))
    engine.herdr.notification_show = lambda title, body: (engine.herdr.shown.append(title) or {"shown": True, "reason": "ok"})
    engine._external = lambda *a, **k: _Ctx()
    engine.source_toasts = lambda: sb.collect_source_toasts(engine)
    engine.toast_candidates = lambda d, h, n: sb.Switchboard.toast_candidates(engine, d, h, n)
    return engine


class _Ctx:
    def __enter__(self):
        return {}

    def __exit__(self, *a):
        return False


def passes(engine, count):
    discovery = types.SimpleNamespace(agents={})
    for _ in range(count):
        sb.Switchboard.render_toast(engine, discovery, {}, 0, {"toasts": []})


class IntentToastOnce(unittest.TestCase):
    def test_two_pending_edits_are_two_toasts(self):
        engine = world(FakeSource(["intent edit #1 t1", "intent edit #2 t2"]))
        passes(engine, 6)
        self.assertEqual(len(engine.herdr.shown), 2, "an Intent edit was toasted again")

    def test_new_stamp_is_toasted(self):
        source = FakeSource(["intent edit #1 t1"])
        engine = world(source)
        passes(engine, 2)
        source.reasons = ["intent edit #1 t2"]
        passes(engine, 2)
        self.assertEqual(len(engine.herdr.shown), 2)

    def test_refused_toast_is_still_owed(self):
        engine = world(FakeSource(["intent edit #1 t1"]))
        engine.herdr.notification_show = lambda t, b: {"shown": False, "reason": "rate_limited"}
        passes(engine, 2)
        self.assertNotIn(("cos", "intent edit #1 t1"), engine.toasted_edits)
        engine.herdr.notification_show = lambda t, b: (engine.herdr.shown.append(t) or {"shown": True, "reason": "ok"})
        passes(engine, 2)
        self.assertEqual(len(engine.herdr.shown), 1)


if __name__ == "__main__":
    unittest.main()
