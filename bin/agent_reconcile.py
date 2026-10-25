"""Reconcile on read (slice 4 of #903): what a view does with an agent that has no live data, and which states a view may draw.

Three rules, all from docs/agent-registry.md:

1. A view that finds an agent with no pane and no process checks it RIGHT THEN (`live_check`) and, when it really is gone, removes it through
   `registry.reconcile_remove`. The registry checks the evidence itself and audits the removal. Nothing else removes an agent this way, and no
   scheduled sweep does (a test derives the callers of `reconcile_remove` from the code).
2. Only a WHOLE read acts (the #805 rule): a partial or failed read removes nothing.
3. A view never prints "?" for an agent's state and never renders the state "dead": it draws a live state or nothing (status and the sidebar
   do; tests pin all five views).

The registry module is passed in (default: the sibling `registry.py`), so a test can hand in a small fake of the same API.
"""
import importlib.util
import os
import sys
import time

DEAD = "dead"
# What a view may draw. `dead` is removed, not drawn; anything not listed (an unknown word, a missing state) is drawn as nothing.


def _load_registry():
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "registry.py")
    spec = importlib.util.spec_from_file_location("registry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evidence_of(agent, live):
    """The evidence handed to the registry: the recorded state, and what the live check found. `live` is {"pane": bool|None, "process": bool|None}
    where None means "could not be checked" (then there is no evidence of death and nothing is removed)."""
    return {"state": agent.get("state"), "pane_listed": live.get("pane"), "process_found": live.get("process"), "checked_at": int(time.time())}


def has_no_live_data(agent, live):
    """True only when both the pane and the process were checked and are both absent. An unchecked part (None) is not absence."""
    return live.get("pane") is False and live.get("process") is False


def unknown_endpoint_check(name, agent):
    """The default check of a no-pane row's endpoint: "unknown", never dead. A cloud lane's feeder supplies a real one (a bounded probe or a lease)."""
    return "unknown"


def has_no_pane(agent):
    """A row with an endpoint and no pane: its liveness is not provable with the terminal multiplexer."""
    return _load_registry().no_pane_to_judge(agent)


# TODO(#1066, PR #1075): when the process-evidence verdict lands, this and `has_no_live_data` must call that ONE dead-verdict function (U6).
def reconcile_on_read(state_dir, read, live_check, registry=None, endpoint_check=None):
    """Called by a view with the registry read it is drawing from. Returns the names removed.

    Acts only on a whole read. For each agent whose recorded state is `dead` (the registry refuses any other), it
    checks right now (`live_check(name, agent)` -> {"pane":, "process":}) and, when both are gone, calls `registry.reconcile_remove`.
    The registry decides again from the evidence: a refusal leaves the agent in place. Never raises: a failure removes nothing."""
    if not isinstance(read, dict) or read.get("whole") is not True:
        return []
    registry = registry or _load_registry()
    removed = []
    for name, agent in sorted((read.get("agents") or {}).items()):
        if not isinstance(agent, dict):
            continue
        if registry.no_pane_to_judge(agent):
            # Unknown, never dead: removed only when the bounded check answers exactly "dead" (a failed, unreachable or unknown answer keeps the row).
            try:
                verdict = (endpoint_check or unknown_endpoint_check)(name, agent)
            except Exception:
                continue
            if verdict != "dead":
                continue
            try:
                result = registry.reconcile_remove(state_dir, name, {"endpoint_verdict": "dead", "checked_at": int(time.time())}, True)
            except Exception:
                continue
            if isinstance(result, dict) and result.get("changed"):
                removed.append(name)
            continue
        if agent.get("state") != DEAD:
            continue   # the registry removes only an agent recorded dead; the pane pass records it, a view never marks one
        try:
            live = live_check(name, agent) or {}
        except Exception:
            continue   # a check that cannot be made is no evidence
        if not has_no_live_data(agent, live):
            continue
        try:
            result = registry.reconcile_remove(state_dir, name, evidence_of(agent, live), True)
        except Exception:
            continue
        if isinstance(result, dict) and result.get("changed"):
            removed.append(name)
    return removed


if __name__ == "__main__":
    sys.exit("agent_reconcile is a library module")
