"""The Chief of Staff's clear under the owner's standing order (#661).

The owner's own `impact clear` lifts an impact hold at the pull request's head. The owner can also order, as a numbered standing order on a charter, that the Chief of Staff may clear
ordinary held pull requests and tell him after. This module is the CHECK of that delegated clear (the command and its audit are in `bin/fleet-switchboard`); it decides nothing else.
It is OFF unless the configuration (`delegated_clear`) names the order numbers that delegate a clear and the charter that holds them. It refuses: any caller but the Chief of Staff, an
order that is not listed or does not exist, a pull request whose owner-hold decision names a path of a class the matrix lists as never delegated (credentials, identity), one whose
independent review's class is a top tier class (credentials, identity or authority), one that changes ANY file of the clear code or of what it depends on (the daemon file included),
and anything it cannot read.

Pure: plain values in, a list of reasons out (empty means allowed). The classes and the clear code are DATA in the matrix file (`owner_hold.delegated_clear`): this module holds no path or class.
"""
import hashlib
import re

CONFIG_KEY = "delegated_clear"   # the module declares its own configuration key (bin/fleet-switchboard config_declarations)
CONFIG_DEFAULT = None            # None: no default is added; absent = off
MAX_ORDERS = 20


def order_hash(text):
    """The hash the configuration binds an order to: sha256 of the order's text (the bullet as `orders list --json` shows it), so an edit, an insertion or a removal above it revokes the delegation."""
    return hashlib.sha256(str(text).strip().encode("utf-8")).hexdigest()


def _entry_ok(entry):
    return (isinstance(entry, dict) and set(entry) == {"number", "sha256"} and isinstance(entry["number"], int) and not isinstance(entry["number"], bool) and entry["number"] >= 1
            and isinstance(entry["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is not None)


def validate(raw, source):
    """`delegated_clear`: absent, null or false = off; else {"orders": [{"number": N, "sha256": "<hash of the order's text>"}, ...], "charter": the charter issue number that holds them}. An empty
    list is off. Raises ValueError naming the key."""
    if raw is None or raw is False:
        return
    if not isinstance(raw, dict) or set(raw) - {"orders", "charter"}:
        raise ValueError("%s: 'delegated_clear' must be false or an object with only orders and charter, got %r" % (source, raw))
    orders, charter = raw.get("orders"), raw.get("charter")
    if not (isinstance(orders, list) and len(orders) <= MAX_ORDERS and all(_entry_ok(e) for e in orders)) or len({e["number"] for e in orders}) != len(orders):
        raise ValueError("%s: delegated_clear.orders must be a list of {\"number\": N, \"sha256\": <64 hex of the order's text>} (an empty list is off; a number may appear once)" % source)
    if not (isinstance(charter, int) and not isinstance(charter, bool) and charter >= 1):
        raise ValueError("%s: delegated_clear.charter must be the charter's issue number" % source)


def settings(config):
    """({number: sha256}, charter) when the delegated clear is configured and well formed, else None: not enabled (absent, empty, malformed, false: all off)."""
    raw = (config or {}).get(CONFIG_KEY) if isinstance(config, dict) else None
    try:
        validate(raw, "config")
    except ValueError:
        return None
    return ({e["number"]: e["sha256"] for e in raw["orders"]}, raw["charter"]) if isinstance(raw, dict) and raw["orders"] else None


def caller_is_chief(kind):
    """True for the Chief of Staff's own session: `kind` is `gates_caller_kind`'s answer (an agent whose RECORDED role is chief-of-staff). Never read from a flag or text."""
    return isinstance(kind, tuple) and len(kind) > 2 and kind[0] == "agent" and kind[2] == "chief-of-staff"


def order_text(orders, number):
    """The text of the 1-based order `number` of a charter's `## Standing orders`, or None when there is no such order."""
    if isinstance(number, int) and not isinstance(number, bool) and isinstance(orders, list) and 1 <= number <= len(orders) and isinstance(orders[number - 1], str):
        return orders[number - 1]
    return None


def refusals(rule, report, files, matches=None, matches_named=None):
    """[reasons] a delegated clear is refused for this pull request; [] when none. `rule` is the matrix's `owner_hold.delegated_clear` block; `report` is the owner-hold report the
    daemon uses (`pr_floor_report`: its decision, its top tier path hits by class, the independent review's class, whether the file list is complete); `files` is the pull request's
    changed files ([{"filename", "previous_filename"}]) or None when they could not be read. `matches(path, globs)` and `matches_named(path, globs)` are the owner-hold floor's OWN
    path matchers (case-, normalisation- and spelling-insensitive: `./bin/x`, `bin//x`, `BIN/X` and a `..` all read as the file they name): the caller passes them, this module holds no second one."""
    if not isinstance(rule, dict) or not isinstance(report, dict) or matches is None or matches_named is None:
        return ["the delegated-clear rule or the owner-hold decision cannot be read"]
    for key in ("never_path_classes", "top_tier_review_classes", "clear_code_paths", "clear_code_exclude"):
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
    if files is None:
        reasons.append("the changed files cannot be read, so a change to the clear code cannot be ruled out")
        return reasons
    paths = set(report.get("changed_paths") or [])
    for item in files:
        if isinstance(item, dict):
            paths |= {item.get("filename"), item.get("previous_filename")}
    for path in sorted(p for p in paths if p is not None):
        if not isinstance(path, str) or "\x00" in path:
            reasons.append("a changed path cannot be read (%r): refused" % str(path)[:60])
        elif matches(path, rule["clear_code_paths"]) and not matches_named(path, rule["clear_code_exclude"]):
            reasons.append("it adds, changes, deletes or renames a file the clear command or the daemon runs or loads (%s): that stays the owner's own clear" % path[:80])
    return reasons
