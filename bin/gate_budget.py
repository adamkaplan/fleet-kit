"""(#2215 S4) The default inner budget of `gates-check`, derived from the gh shim's limit (shims/gh GATE_SECONDS, moved by FLEET_SHIM_GATE_SECONDS)."""
import os
import re

OLD_DEFAULT_MS = 3000   # the pinned number this replaces; kept only as the last resort when the shim cannot be read
MARGIN_S = 1.0          # what the check keeps back from the shim's limit for process start and the answer's travel
FLOOR_MS = 500
SHIM_GH = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "shims", "gh")


def shim_gate_seconds():
    """The shim's limit on one gates-check, read from the source the shim uses and clamped as its gate_seconds() does; None if unreadable."""
    try:
        with open(SHIM_GH, encoding="utf-8") as handle:
            default = float(re.search(r"^GATE_SECONDS = ([0-9.]+)", handle.read(), re.M).group(1))
    except (OSError, AttributeError, ValueError):
        return None
    try:
        return min(10.0, max(0.2, float(os.environ.get("FLEET_SHIM_GATE_SECONDS", default))))
    except ValueError:
        return default


def default_budget_ms():
    """The inner budget when a repo sets none: the shim's limit minus MARGIN_S. A `budget_ms` in the config always wins."""
    limit = shim_gate_seconds()
    if limit is None:
        return OLD_DEFAULT_MS
    return max(FLOOR_MS, int((limit - MARGIN_S) * 1000))
