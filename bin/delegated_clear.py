"""The Chief of Staff's clear under the owner's standing order (#661).

The owner's own `impact clear` lifts an impact hold at the pull request's head. The owner can also order, as a numbered standing order on a charter, that the Chief of Staff may clear
ordinary held pull requests and tell him after. This module is the CHECK of that delegated clear (the command and its audit are in `bin/fleet-switchboard`); it decides nothing else.
It is OFF unless the configuration (`delegated_clear`) names the order numbers that delegate a clear and the charter that holds them. It refuses: any caller but the Chief of Staff, an
order that is not listed or does not exist, a pull request whose owner-hold decision names a path of a class the matrix lists as never delegated (credentials, identity), one whose
independent review's class is a top tier class (credentials, identity or authority), one that changes the clear code, and anything it cannot read.

Pure: plain values in, a list of reasons out (empty means allowed). The classes and the clear code are DATA in the matrix file (`owner_hold.delegated_clear`): this module holds no path or class.
"""
import re

CONFIG_KEY = "delegated_clear"   # the module declares its own configuration key (bin/fleet-switchboard config_declarations)
CONFIG_DEFAULT = None            # None: no default is added; absent = off
MAX_ORDERS = 20


def validate(raw, source):
    """`delegated_clear`: absent, null or false = off; else {"orders": [1-based order numbers], "charter": the charter issue number that holds them}. Raises ValueError naming the key."""
    if raw is None or raw is False:
        return
    if not isinstance(raw, dict) or set(raw) - {"orders", "charter"}:
        raise ValueError("%s: 'delegated_clear' must be false or an object with only orders and charter, got %r" % (source, raw))
    orders, charter = raw.get("orders"), raw.get("charter")
    if not (isinstance(orders, list) and len(orders) <= MAX_ORDERS and all(isinstance(n, int) and not isinstance(n, bool) and n >= 1 for n in orders)):
        raise ValueError("%s: delegated_clear.orders must be a list of order numbers (1 or more; an empty list is off)" % source)
    if not (isinstance(charter, int) and not isinstance(charter, bool) and charter >= 1):
        raise ValueError("%s: delegated_clear.charter must be the charter's issue number" % source)


def settings(config):
    """(orders, charter) when the delegated clear is configured and well formed, else None: not enabled (absent, empty, malformed, false: all off)."""
    raw = (config or {}).get(CONFIG_KEY) if isinstance(config, dict) else None
    try:
        validate(raw, "config")
    except ValueError:
        return None
    return (list(raw["orders"]), raw["charter"]) if isinstance(raw, dict) and raw["orders"] else None


def caller_is_chief(kind):
    """True for the Chief of Staff's own session: `kind` is `gates_caller_kind`'s answer (an agent whose RECORDED role is chief-of-staff). Never read from a flag or text."""
    return isinstance(kind, tuple) and len(kind) > 2 and kind[0] == "agent" and kind[2] == "chief-of-staff"


def order_text(orders, number):
    """The text of the 1-based order `number` of a charter's `## Standing orders`, or None when there is no such order."""
    if isinstance(number, int) and not isinstance(number, bool) and isinstance(orders, list) and 1 <= number <= len(orders) and isinstance(orders[number - 1], str):
        return orders[number - 1]
    return None


def _symbol_changes(patch, symbols):
    """The clear-code symbols a diff mentions in a hunk header (the function around the change) or in a changed line."""
    found = set()
    for line in str(patch or "").splitlines():
        if line.startswith("@@") or line[:1] in "+-":
            found |= {s for s in symbols if re.search(r"\b%s\b" % re.escape(s), line)}
    return sorted(found)


def refusals(rule, report, files):
    """[reasons] a delegated clear is refused for this pull request; [] when none. `rule` is the matrix's `owner_hold.delegated_clear` block; `report` is the owner-hold report the
    daemon uses (`pr_floor_report`: its decision, its top tier path hits by class, the independent review's class, whether the file list is complete); `files` is the pull request's
    changed files with their patches ([{"filename", "patch"}]) or None when they could not be read."""
    if not isinstance(rule, dict) or not isinstance(report, dict):
        return ["the delegated-clear rule or the owner-hold decision cannot be read"]
    for key in ("never_path_classes", "top_tier_review_classes", "clear_code_paths", "clear_code_symbols", "clear_code_symbol_files"):
        if not isinstance(rule.get(key), list) or not all(isinstance(x, str) for x in rule[key]):
            return ["the delegated-clear rule is malformed (%s)" % key]
    if report.get("owner_decision") not in ("hold", "second-review", "none"):
        return ["the owner-hold decision cannot be read"]
    if report.get("complete") is False or report.get("unknown") or report.get("renamed_no_source"):
        return ["the pull request's changes cannot be read in full (%s)" % (report.get("unknown") or "the file list is cut")]
    reasons = []
    hits = report.get("top_tier_hits") or {}
    for cls in sorted(hits):
        if cls.lower() in {c.lower() for c in rule["never_path_classes"]}:
            reasons.append("it changes a %s path (%s): that stays the owner's own clear" % (cls, ", ".join(hits[cls][:3])))
    if report.get("review_action") is None:
        reasons.append("no independent review is recorded at this head: unknown is refused")
    review = report.get("review_class")
    if review is None and report.get("review_action") == "hold":
        reasons.append("the independent review holds it with no class named: unknown is refused")
    if isinstance(review, str) and review.lower() in {c.lower() for c in rule["top_tier_review_classes"]}:
        reasons.append("the independent review's class is %s, a top tier class: that stays the owner's own clear" % review)
    changed = {f.get("filename") for f in files or [] if isinstance(f, dict)} | {f.get("previous_filename") for f in files or [] if isinstance(f, dict)} | set(report.get("changed_paths") or [])
    for path in sorted(p for p in changed if isinstance(p, str)):
        if path in rule["clear_code_paths"]:
            reasons.append("it changes the clear code (%s): a change to the clear is the owner's own" % path)
    if files is None:
        reasons.append("the patches of the changed files cannot be read, so a change to the clear code cannot be ruled out")
    else:
        for item in files:
            if not isinstance(item, dict) or not ({item.get("filename"), item.get("previous_filename")} & set(rule["clear_code_symbol_files"])):
                continue
            if not isinstance(item.get("patch"), str):
                reasons.append("the patch of %s is not available, so a change to the clear code cannot be ruled out" % item.get("filename"))
                continue
            found = _symbol_changes(item["patch"], rule["clear_code_symbols"])
            if found:
                reasons.append("it changes the clear code in %s (%s): a change to the clear is the owner's own" % (item["filename"], ", ".join(found[:4])))
    return reasons
