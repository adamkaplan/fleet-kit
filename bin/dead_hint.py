"""dead_hint: what the switchboard knows about a DEAD agent and what to do about it (#379). Pure text; reads nothing, writes nothing.

`hint(name, entry, now, boss=None)` takes the agent's `last-reports.json` entry (or None) and says either
`last report: <state> <age> ago` or `no report since launch`, then how to retire it (it finished on purpose) or bring it back.
The newest sign of life counts: a report to anyone, or a send to the boss (to anyone, when the boss is not known). The text is
bounded, whatever the state or the name holds. Python 3.9+, standard library only; loaded by path from beside bin/fleet-switchboard.
"""

import re

PROG = "fleet-switchboard"
MAX_LENGTH = 240
_PLAIN = re.compile(r"[^A-Za-z0-9._:@/# -]")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _plain(text, limit):
    return _PLAIN.sub("?", str(text))[:limit]


def age_text(seconds):
    """The age as the other wakes print it (`_age_text` in the switchboard), plus days for a long-dead agent."""
    if seconds < 120:
        return "%ds" % seconds
    if seconds < 7200:
        return "%dm" % (seconds // 60)
    if seconds < 172800:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)


def last_sign(entry, now, boss=None):
    """(label, epoch) of the newest sign of life in a last-reports entry, or None. A time in the future counts for nothing."""
    if not isinstance(entry, dict):
        return None
    signs = []
    if _number(entry.get("at")) and entry["at"] <= now + 60:
        signs.append((_plain(entry.get("state") or "report", 30), float(entry["at"])))
    sends = entry.get("send_to") if isinstance(entry.get("send_to"), dict) else {}
    for who, at in list(sends.items())[:50]:   # bounded: the file is small, but a long map is not walked in full
        if (boss is None or who == boss) and _number(at) and at <= now + 60:
            signs.append(("a message sent", float(at)))
    return max(signs, key=lambda s: s[1]) if signs else None


def hint(name, entry, now, boss=None):
    """One bounded sentence for a DEAD agent. `last_sign` is the same rule as the reminder wake's `last_report` (a test holds them together)."""
    sign = last_sign(entry, now, boss)
    known = "no report since launch" if sign is None else "last report: %s %s ago" % (sign[0], age_text(max(0, int(now - sign[1]))))
    name = _plain(name, 60)
    text = "%s; if it finished on purpose: %s retire %s; to bring it back: %s reattach %s" % (known, PROG, name, PROG, name)
    return text[:MAX_LENGTH]
