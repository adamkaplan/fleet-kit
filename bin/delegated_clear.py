"""The Chief of Staff's clear under the owner's standing order (#661).

The owner's own `impact clear` lifts an impact hold at the pull request's head. The owner can also order, as a numbered standing order on a charter, that the Chief of Staff may clear
ordinary held pull requests and tell him after. This module is the CHECK of that delegated clear (the command and its audit are in `bin/fleet-switchboard`); it decides nothing else.
It is OFF unless the configuration (`delegated_clear`) names the order numbers that delegate a clear and the charter that holds them. It refuses: any caller but the Chief of Staff, an
order that is not listed or does not exist, a pull request whose owner-hold decision names a path of a class the matrix lists as never delegated (credentials, identity), one whose
independent review's class is a top tier class (credentials, identity or authority), one that changes ANY file of the clear code or of what it depends on (the daemon file included),
and anything it cannot read.

Pure: plain values in, a list of reasons out (empty means allowed). The classes and the clear code are DATA in the matrix file (`owner_hold.delegated_clear`): this module holds no path or class.
"""
import ast
import base64
import difflib
import hashlib
import json
import re
from pathlib import Path

CONFIG_KEY = "delegated_clear"   # the module declares its own configuration key (bin/fleet-switchboard config_declarations)
CONFIG_DEFAULT = None            # None: no default is added; absent = off
MAX_ORDERS = 20


MATRIX_FILE = Path(__file__).resolve().parent.parent / "docs" / "review-laws" / "impact-matrix.json"
OWNER_FILE = "owner-file"   # an order's `source` when its text is a line of the owner's own standing-orders file (never written by the fleet), not an order of a charter


def score_block():
    """The matrix file's `owner_hold.delegated_clear.score` block (the ladder, the delegable levels, the floor's rule ids and lowest level, the roles whose rating counts), or None when it cannot be read. #835."""
    try:
        block = json.loads(MATRIX_FILE.read_text())["owner_hold"]["delegated_clear"]["score"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    ok = (isinstance(block, dict) and all(isinstance(block.get(k), list) and block[k] and all(isinstance(x, str) for x in block[k]) for k in ("ladder", "delegable_levels", "rating_roles"))
          and isinstance(block.get("floor"), dict) and isinstance(block["floor"].get("lowest_level"), str) and isinstance(block["floor"].get("rules"), dict)
          and all(isinstance(k, str) and isinstance(v, str) for k, v in block["floor"]["rules"].items()))
    return block if ok else None


def order_hash(text):
    """The hash the configuration binds an order to: sha256 of the order's text (the bullet as `orders list --json` shows it), so an edit, an insertion or a removal above it revokes the delegation."""
    return hashlib.sha256(str(text).strip().encode("utf-8")).hexdigest()


def line_hash(line):
    """The hash a file-sourced order is bound to: sha256 of its EXACT line, trimmed of its line end only (no case folding, no other trimming)."""
    return hashlib.sha256(str(line).rstrip("\r\n").encode("utf-8")).hexdigest()


def file_order_line(text, sha):
    """The ONE line of the owner's standing-orders text whose line_hash is `sha`, or None: zero lines or more than one match, or a blank line, is None (the order is absent). No fuzzy, substring or case-folded match."""
    if not isinstance(text, str):
        return None
    found = [line.rstrip("\r\n") for line in text.split("\n") if line.strip() and line_hash(line) == sha]
    return found[0] if len(found) == 1 else None


def _entry_ok(entry):
    return (isinstance(entry, dict) and set(entry) <= {"number", "sha256", "max_level", "source"} and {"number", "sha256"} <= set(entry)
            and isinstance(entry["number"], int) and not isinstance(entry["number"], bool) and entry["number"] >= 1
            and isinstance(entry["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is not None
            and entry.get("source", OWNER_FILE) == OWNER_FILE)


def _level_problem(entry):
    """What is wrong with an entry's `max_level` (None when it is absent: clear nothing by score, or valid): a level the matrix does not list as delegable (High never is) is refused by name."""
    if "max_level" not in entry:
        return None
    block = score_block()
    if block is None:
        return "the matrix file's score block cannot be read"
    if not isinstance(entry["max_level"], str) or entry["max_level"] not in block["delegable_levels"]:
        return "must be one of %s (High is always the owner's), got %r" % (", ".join(block["delegable_levels"]), entry["max_level"])
    return None


def validate(raw, source):
    """`delegated_clear`: absent, null or false = off; else {"orders": [{"number": N, "sha256": "<hash of the order's text>", "max_level": "<highest level she may clear>"?, "source": "owner-file"?}, ...], "charter": the
    charter issue number that holds the charter-sourced orders (not needed when every order is `owner-file`)}. An empty list is off. `max_level` absent = the old rules (nothing is cleared by score). Raises ValueError naming the key."""
    if raw is None or raw is False:
        return
    if not isinstance(raw, dict) or set(raw) - {"orders", "charter"}:
        raise ValueError("%s: 'delegated_clear' must be false or an object with only orders and charter, got %r" % (source, raw))
    orders, charter = raw.get("orders"), raw.get("charter")
    if not (isinstance(orders, list) and len(orders) <= MAX_ORDERS and all(_entry_ok(e) for e in orders)) or len({e["number"] for e in orders}) != len(orders):
        raise ValueError("%s: delegated_clear.orders must be a list of {\"number\": N, \"sha256\": <64 hex of the order's text>, \"max_level\"?, \"source\"?: \"owner-file\"} (an empty list is off; a number may appear once)" % source)
    for entry in orders:
        problem = _level_problem(entry)
        if problem:
            raise ValueError("%s: delegated_clear.orders max_level of order %d: %s" % (source, entry["number"], problem))
    if any("source" not in e for e in orders) and not (isinstance(charter, int) and not isinstance(charter, bool) and charter >= 1):
        raise ValueError("%s: delegated_clear.charter must be the charter's issue number" % source)
    if charter is not None and not (isinstance(charter, int) and not isinstance(charter, bool) and charter >= 1):
        raise ValueError("%s: delegated_clear.charter must be the charter's issue number" % source)


def settings(config):
    """({number: sha256}, charter) when the delegated clear is configured and well formed, else None: not enabled (absent, empty, malformed, false: all off)."""
    raw = (config or {}).get(CONFIG_KEY) if isinstance(config, dict) else None
    try:
        validate(raw, "config")
    except ValueError:
        return None
    return ({e["number"]: e["sha256"] for e in raw["orders"]}, raw.get("charter")) if isinstance(raw, dict) and raw["orders"] else None


def entry_of(config, number):
    """The configured order entry `number` ({"number", "sha256", "max_level"?, "source"?}) when the delegated clear is enabled and well formed, else None."""
    if settings(config) is None:
        return None
    return next((e for e in config[CONFIG_KEY]["orders"] if e["number"] == number), None)


def caller_is_chief(kind):
    """True for the Chief of Staff's own session: `kind` is `gates_caller_kind`'s answer (an agent whose RECORDED role is chief-of-staff). Never read from a flag or text."""
    return isinstance(kind, tuple) and len(kind) > 2 and kind[0] == "agent" and kind[2] == "chief-of-staff"


def order_text(orders, number):
    """The text of the 1-based order `number` of a charter's `## Standing orders`, or None when there is no such order."""
    if isinstance(number, int) and not isinstance(number, bool) and isinstance(orders, list) and 1 <= number <= len(orders) and isinstance(orders[number - 1], str):
        return orders[number - 1]
    return None


NO_AGENT_BLOCK = ("no agent block: the impact rating at this head carries no verdict block of the reviewer-impact agent for this head that agrees with its score (typed flags, a block for another head or a malformed one), "
                  "so it clears nothing by score; record the agent's block with `review record --reviewer impact --block <file>`")   # #901: the ONE line the clear's refusal, `impact floor` and `gates` all say
BLOCK_HASH_RE = re.compile(r"[0-9a-f]{64}")


def counts_by_block(entry):
    """True when the record carries the block the agent returned: a sha256 of it (`block_sha256`), and its recorded severity and probability are exactly the block's own lines (`block_impact_severity`,
    `block_impact_probability`, kept by `review record --block`). A flags-only record (`block_sha256` is `none`), a record made before blocks were kept, a malformed hash or a typed score that disagrees with the block does
    not count. A mistake-guard: the hash proves a block was given, not that the agent produced it. #901."""
    if not isinstance(entry, dict):
        return False
    digest = entry.get("block_sha256")
    if not isinstance(digest, str) or not BLOCK_HASH_RE.fullmatch(digest):
        return False
    return (isinstance(entry.get("impact_severity"), str) and entry.get("impact_severity") == entry.get("block_impact_severity")
            and isinstance(entry.get("impact_probability"), str) and entry.get("impact_probability") == entry.get("block_impact_probability"))


INCOMPLETE_VERDICT = ("the independent impact rating at this head is an `incomplete` verdict: the rater said it did not read the whole change, so it clears nothing by score; "
                      "re-run the rating and record a complete one")   # #901 clause 8: one line, said by the clear's refusal, `impact floor` and `gates`


HIGHER_UNCOUNTED = ("a rating at this head that does not count by score (no agent block, or an incomplete verdict) is higher than the score that counts, so the clear is refused: "
                    "re-record that rating with the agent's complete block, or it stays the owner's")   # #901: an uncounted rating is never silently dropped when it is the higher one


def uncounted_line(entries):
    """The one line saying why records that exist do not count by score (`NO_AGENT_BLOCK` or `INCOMPLETE_VERDICT`), or None when one counts. `entries` are the independent reviewer's records at the head, already filtered by role. The machine reads only the verdict WORD, never prose."""
    mine = [e for e in entries if isinstance(e, dict)]
    if not mine or any(counts_by_block(e) and e.get("verdict") != "incomplete" for e in mine):
        return None
    return INCOMPLETE_VERDICT if any(counts_by_block(e) and e.get("verdict") == "incomplete" for e in mine) else NO_AGENT_BLOCK


RUNS_REQUIRED = 2   # #972: a clear by score rests on this many counted ratings from different sessions at the head; the ONE place the number is written
TWO_RUN_RULE = ("A clear by score needs two independent impact ratings at the head, from two different rater sessions, each carrying the agent's own block; "
                "the reason of the clear names both runs, each as `<record id or rater>=<cell>`.")   # #972: stated once in the docs and once in each rater brief, byte for byte


def judge(block, cells, entries, report, threshold, runs_required=1):
    """{"level": the score or None, "reasons": [why refused]} (no reasons: the score is at or below `threshold`, so the clear may go on). `block` is the matrix's score block, `cells` its `cells` (severity ->
    probability -> {"level"}), `entries` the ledger's entries (the caller filtered nothing: this checks kind, reviewer, pr, EXACT head and the recorded role again), `report` the owner-hold report (`pr`, `head`,
    `top_tier_hits` {class: [paths]}). The score is the HIGHEST level of the counted records (a later, lower one never lowers it), taken from the matrix cell of the recorded severity and probability, never from
    a word of the reviewer's. A floor class hit makes `floor.lowest_level` the lowest score allowed, and the rating's reason must name that class's rule id. Anything it cannot read is refused, never Low. #835."""
    if not isinstance(block, dict) or not isinstance(cells, dict) or not isinstance(report, dict):
        return {"level": None, "reasons": ["the score rule or the rating is unknown: refused"]}
    ladder = block["ladder"]
    if threshold not in block["delegable_levels"] or threshold not in ladder:
        return {"level": None, "reasons": ["the threshold %r is unknown: refused" % (threshold,)]}
    counted, bad, unblocked, incomplete, uncounted_idx = [], 0, 0, 0, []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("kind") != "review_record" or entry.get("reviewer") != "impact" or entry.get("pr") != report.get("pr") or entry.get("head") != report.get("head") or not report.get("head"):
            continue
        if entry.get("by_role") not in block["rating_roles"]:
            continue   # the pull request's author, the Chief of Staff, any other role (or a record made before roles were kept) is not the independent rating
        if not counts_by_block(entry) or entry.get("verdict") == "incomplete":
            for sev_key, prob_key in (("impact_severity", "impact_probability"), ("block_impact_severity", "block_impact_probability")):   # the typed score AND the block's own lines: the higher of the two is remembered
                sev, prob = entry.get(sev_key), entry.get(prob_key)
                seen = ((cells.get(sev) or {}).get(prob) or {}).get("level") if isinstance(sev, str) and isinstance(prob, str) else None
                if seen in ladder:
                    uncounted_idx.append(ladder.index(seen))   # remembered: a higher uncounted rating refuses (below)
        if not counts_by_block(entry):
            unblocked += 1   # #901: only the agent's own block counts by score. A record without one is IGNORED on purpose (it counts for nothing, as every record made before blocks were kept), so a valid blocked record beside it still clears; the guard is `counts_by_block`, a mistake-guard, and a blocked record whose score the matrix does not know still refuses (`bad`)
            continue
        if entry.get("verdict") == "incomplete":
            incomplete += 1   # #901 clause 8: a rater that read only part of the change says `incomplete`: it counts for nothing
            continue
        level = ((cells.get(entry.get("impact_severity")) or {}).get(entry.get("impact_probability")) or {}).get("level") if isinstance(entry.get("impact_severity"), str) and isinstance(entry.get("impact_probability"), str) else None
        if level not in ladder:
            bad += 1
        else:
            counted.append((ladder.index(level), level, entry))
    counted.sort(key=lambda c: (-c[0], c[2].get("seq") if isinstance(c[2].get("seq"), int) else 0))
    if bad:
        return {"level": None, "reasons": ["a rating at this head has no severity and probability the matrix knows: the score is unknown, refused"]}
    if not counted and unblocked:
        return {"level": None, "reasons": [NO_AGENT_BLOCK]}
    if not counted and incomplete:
        return {"level": None, "reasons": [INCOMPLETE_VERDICT]}
    if not counted:
        return {"level": None, "reasons": ["there is no independent impact rating at this head: the score is unknown, refused"]}
    top = max(i for i, _, _ in counted)
    level = ladder[top]
    reasons = [HIGHER_UNCOUNTED] if uncounted_idx and max(uncounted_idx) > top else []
    hits = report.get("top_tier_hits") or {}
    for rule_id, cls in sorted(block["floor"]["rules"].items()):
        if not hits.get(cls):
            continue
        if top < ladder.index(block["floor"]["lowest_level"]):
            reasons.append("it changes a %s path and the rating %s is below the floor %s: refused" % (cls, level, block["floor"]["lowest_level"]))
        elif not any(i == top and re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(rule_id), str(e.get("impact_reason") or "")) for i, _, e in counted):
            reasons.append("it changes a %s path and the rating's reason does not name the rule %s: the rule is not named, the rating does not count" % (cls, rule_id))
    over = level_refusal(block, level, threshold)
    if over:
        reasons.append(over)
    runs = pick_runs(counted)
    if not reasons and runs_required > 1:   # #972: only a score the order would allow is asked for its corroboration; a score above the threshold is refused by score, not by count
        head8 = str(report.get("head"))[:8]
        sessions = {run_name(e.get("by")) for _, _, e in counted if run_name(e.get("by"))}
        if len(counted) < runs_required:
            reasons.append("%d counted impact rating%s at %s, %d required: a clear by score needs a second independent run at this head with the agent's block" % (len(counted), "" if len(counted) == 1 else "s", head8, runs_required))
        elif len(sessions) < runs_required:
            reasons.append("%d session found rating at %s (%d counted ratings), %d required: the runs must come from different rater sessions (a name differing only by case or spaces is the same session)" % (len(sessions), head8, len(counted), runs_required))
        elif len(runs) < runs_required:
            reasons.append("%d independent run found at %s (%d counted ratings), %d required: two records with the same block hash are one run, and a record with no ledger id cannot be named" % (len(runs), head8, len(counted), runs_required))
    return {"level": level, "reasons": reasons, "runs": runs}


def run_name(by):
    """The rater's recorded name as a run identity: stripped and lower-cased; "" when it is not a name. #972."""
    return by.strip().lower() if isinstance(by, str) else ""


def run_identity(run):
    """(name, block hash) of a run: the ONE place a run's identity is decided. Two runs are independent only when BOTH parts differ. #972."""
    return run_name(run.get("by")), run.get("hash")


def runs_independent(runs):
    """True when `runs` are exactly RUNS_REQUIRED runs, each with a name, a ledger id and a block hash, whose names AND hashes are all distinct. #972."""
    if not isinstance(runs, list) or len(runs) != RUNS_REQUIRED or not all(isinstance(r, dict) for r in runs):
        return False
    ids = [run_identity(r) for r in runs]
    return (all(n and isinstance(h, str) and h for n, h in ids) and all(isinstance(r.get("id"), int) and not isinstance(r.get("id"), bool) for r in runs)
            and len({n for n, _ in ids}) == len(ids) and len({h for _, h in ids}) == len(ids))


def pick_runs(counted):
    """[{"id": ledger seq, "by": the recorded session, "hash": the block hash, "cell": the level}] of the runs a clear names: the highest counted record, then the highest counted one whose name AND block hash both differ from every run already picked (one draw recorded twice, or one session under two spellings, is one run). A record with no ledger id is skipped. `counted` is sorted highest first. #972."""
    runs = []
    for _, level, e in counted:
        seq, name, digest = e.get("seq"), run_name(e.get("by")), e.get("block_sha256")
        if not isinstance(seq, int) or isinstance(seq, bool) or not name or any(run_name(r["by"]) == name or r["hash"] == digest for r in runs):
            continue
        runs.append({"id": seq, "by": e.get("by"), "hash": digest, "cell": level})
        if len(runs) == RUNS_REQUIRED:
            break
    return runs


def reason_problems(reason, runs):
    """[problems] unless `runs` are exactly RUNS_REQUIRED independent runs (`runs_independent`) and `reason` names each as `#<id>=<cell>` or `<rater>=<cell>`, every such `name=cell` for a run agreeing with the record's own cell. #972."""
    if not runs_independent(runs):
        return ["the reason cannot be checked: a clear by score needs exactly %d independent runs (distinct block hash and distinct rater name), %d given" % (RUNS_REQUIRED, len(runs) if isinstance(runs, list) else 0)]
    problems = []
    for r in runs:
        found = [m.group(1) for n in ("#%s" % r["id"], r["by"]) for m in re.finditer(r"(?<![\w#-])%s\s*=\s*([A-Za-z]+)" % re.escape(n), reason or "")]
        if not found:
            problems.append("the reason does not name run #%s (%s) with its cell: write `#%s=%s`" % (r["id"], r["by"], r["id"], r["cell"]))
        elif any(c.lower() != str(r["cell"]).lower() for c in found):
            problems.append("the reason names run #%s with the wrong cell %s (its cell is %s)" % (r["id"], next(c for c in found if c.lower() != str(r["cell"]).lower()), r["cell"]))
    return problems


def level_refusal(block, level, threshold):
    """The reason a score `level` is refused whatever the rating's other lines say (not a level a delegation may clear, or above `threshold`), or None. The ONE comparison: `judge` makes it on the score, `who_can_clear` on the floor's lowest level."""
    if level not in block["delegable_levels"]:
        return "the score is %s: %s is the owner's" % (level, level)
    if block["ladder"].index(level) > block["ladder"].index(threshold):
        return "the score %s is above the threshold %s" % (level, threshold)
    return None


def daemon_roots(rule):
    """The daemon file's roots of the clear closure: `closure_roots` (what the clear command runs) and `rating_roots` (the code that WRITES and PARSES the independent rating the score is read from, #835): a change to a definition reachable from any of them is the owner's own clear."""
    return sorted(set(rule["closure_roots"]) | set(rule.get("rating_roots") or []))


def _clean(text):
    """A reason is one printable line: a path comes from a pull request, so a control character in it (an escape, a newline) is replaced, never carried into a posted reason. The ONE place: `_item`."""
    return "".join(c if c.isprintable() else "?" for c in text) if isinstance(text, str) else text


def _item(kind, text, paths=(), **extra):
    return dict({"kind": kind, "text": _clean(text), "paths": [_clean(p) for p in paths]}, **extra)


def refusal_items(rule, report, files, matches=None, closure=None, scored=None):
    """([item], tidy): every refusal of `refusals` as {"kind", "text", "paths", ...} in the order it is found; the ONE place the rules are written. `tidy` is False where the clear has always
    returned the reasons as found (an early stop on something unreadable), True where it returns them sorted and once each. The kinds: `unreadable` (a read or the rule itself failed), `rating` (the score rule),
    `class` (a path class never delegated), `registrar` (#964), `review` (the independent review at this head), `machinery`, `new-file`, `closure`."""
    if not isinstance(rule, dict) or not isinstance(report, dict) or matches is None:
        return [_item("unreadable", "the delegated-clear rule or the owner-hold decision cannot be read", why="the rule or the decision cannot be read")], False
    for key in ("never_path_classes", "top_tier_review_classes", "clear_code_paths", "new_file_globs", "closure_files"):
        if not isinstance(rule.get(key), list) or not all(isinstance(x, str) for x in rule[key]):
            return [_item("unreadable", "the delegated-clear rule is malformed (%s)" % key, why="the clear rule is malformed")], False
    if report.get("owner_decision") not in ("hold", "second-review", "none"):
        return [_item("unreadable", "the owner-hold decision cannot be read", why="the owner-hold decision cannot be read")], False
    if report.get("complete") is not True or report.get("unknown") or report.get("renamed_no_source"):
        return [_item("unreadable", "the pull request's changes cannot be read in full (%s)" % (report.get("unknown") or "the file list is cut"), why="the pull request's changes cannot be read in full")], False
    items = [_item("rating", r) for r in (scored["reasons"] if isinstance(scored, dict) else [])]
    held = report.get("registrar_reasons")   # #964: whatever the rating says
    if not isinstance(held, list) or any(not isinstance(x, str) for x in held):
        items.append(_item("registrar", "the registrar lint's answer cannot be read: refused"))
    else:
        items.extend(_item("registrar", "%s: that stays the owner's own clear" % x[:160]) for x in held)
    hits = report.get("top_tier_hits") or {}
    for cls in sorted(hits) if scored is None else ():
        if cls.lower() in {c.lower() for c in rule["never_path_classes"]}:
            items.append(_item("class", "it changes a %s path (%s): that stays the owner's own clear" % (cls, ", ".join(hits[cls][:3])), hits[cls][:3], cls=cls))
    if report.get("review_action") is None and scored is None:
        items.append(_item("review", "no independent review is recorded at this head: unknown is refused"))
    review = report.get("review_class")
    if review is None and report.get("review_action") == "hold" and scored is None:
        items.append(_item("review", "the independent review holds it with no class named: unknown is refused"))
    if isinstance(review, str) and review.lower() in {c.lower() for c in rule["top_tier_review_classes"]} and scored is None:
        items.append(_item("review", "the independent review's class is %s, a top tier class: that stays the owner's own clear" % review))
    if files is None:
        items.append(_item("unreadable", "the changed files cannot be read, so a change to the clear code cannot be ruled out", why="the changed files cannot be read"))
        return items, False
    named = set(report.get("changed_paths") or [])
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            items.append(_item("unreadable", "a changed file cannot be read: refused", why="a changed file cannot be read"))
            continue
        both = [x for x in (item.get("filename"), item.get("previous_filename")) if x is not None]
        if any(not isinstance(x, str) or "\x00" in x for x in both):
            items.append(_item("unreadable", "a changed path cannot be read (%r): refused" % str(both)[:60], why="a changed path cannot be read"))
            continue
        for path in both:
            if matches(path, rule["clear_code_paths"]):
                items.append(_item("machinery", "it changes a file that is the clear or hold machinery or that the daemon loads (%s): that stays the owner's own clear" % path[:80], [path]))
        if any(matches(path, rule["new_file_globs"]) for path in both) and (item.get("status") in ("added", "removed", "renamed", "copied") or item.get("previous_filename")):
            items.append(_item("new-file", "it adds, deletes or renames a file under a directory the daemon loads at start (%s): that stays the owner's own clear" % item["filename"][:80], [item["filename"]],
                               verb={"removed": "deletes a file", "renamed": "renames a file"}.get(item.get("status") if not item.get("previous_filename") else "renamed", "adds a file")))
        for path in both:
            if matches(path, rule["closure_files"]) or matches(path, ["bin/switchboard/*.py"]):   # a part the closure does not list is judged like a listed one: a file nobody mapped (any status but `modified`) is refused as unmappable, never skipped
                mapped = (closure or {}).get(item["filename"])
                if mapped is None:
                    items.append(_item("unreadable", "the change to %s cannot be mapped to its definitions: refused" % path[:60], why="a change cannot be mapped to its definitions"))
                else:
                    items.extend(_item("closure", reason, [item["filename"]]) for reason in mapped)
    for path in sorted(p for p in named if isinstance(p, str)):
        if matches(path, rule["clear_code_paths"]):
            items.append(_item("machinery", "it changes a file that is the clear or hold machinery or that the daemon loads (%s): that stays the owner's own clear" % path[:80], [path]))
    return items, True


def refusals(rule, report, files, matches=None, closure=None, scored=None):
    """[reasons] a delegated clear is refused for this pull request; [] when none. `rule` is the matrix's `owner_hold.delegated_clear` block; `report` is the owner-hold report the
    daemon uses (`pr_floor_report`: its decision, its top tier path hits by class, the independent review's class, whether the file list is complete); `files` is the pull request's
    changed files ([{"filename", "previous_filename", "status"}]) or None when they could not be read. `matches(path, globs)` is the owner-hold floor's OWN path matcher (case-,
    normalisation- and spelling-insensitive: `./bin/x`, `bin//x`, `BIN/X`, a `..`, a rename's source all read as the file they name): the caller passes it, this module holds no second one.
    `closure` is {file name: [reasons] or None}: what `closure_refusals` said for each changed file of `closure_files` (None: it could not be mapped). `scored` is `judge`'s answer for an order with a
    max_level: then the score is the only rule about the pull request's classes (the path-class and review-class refusals below are not made), and the machinery refusals stay. The rules are written once, in `refusal_items`."""
    items, tidy = refusal_items(rule, report, files, matches, closure, scored)
    reasons = [i["text"] for i in items]
    return sorted(set(reasons)) if tidy else reasons


# ---- one read of a changed daemon file's blobs (#946): the clear and the line the daemon posts read through the SAME cache

MAP_CACHE = {}
MAP_CACHE_MAX = 64


def cached_map(key, compute, take):
    """`compute()` ([reasons] or None) for one changed file, handed from the line to the clear ONCE. The line's dry run (`take` False) reads and keeps a mapped answer; the clear (`take` True) uses a kept answer for
    the same key and removes it, else reads itself and keeps nothing. The key is content-addressed (the head's blob id, the merge-base commit, a hash of the patch, a hash of the rule) and scoped to the state
    directory, so a kept answer cannot be stale; bounded; a failed read is never kept. The line and the clear read a file's blobs once between them, never twice."""
    if take:
        kept = MAP_CACHE.pop(key, None)
        return list(kept) if kept is not None else compute()
    got = compute()
    if got is not None:
        if len(MAP_CACHE) >= MAP_CACHE_MAX:
            MAP_CACHE.pop(next(iter(MAP_CACHE)))
        MAP_CACHE[key] = list(got)
    return got


def map_key(state_dir, repo, item, base_sha, rule):
    """The key of `cached_map` for one changed file."""
    digest = lambda x: hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    return (str(state_dir), repo, item.get("filename"), item.get("sha"), base_sha, digest(item.get("patch")), digest(rule))


# ---- who can clear (#946): the ONE answer, a plain structure, read by the daemon's comment line and by the packet command

WHO = ("no hold", "owner only", "owner (delegation is off)", "chief of staff with a rating", "owner or chief of staff", "unknown")   # the values of `who`
UNREAD = "the pull request's changes or the clear rule could not be read"


def delegation_of(config, block):
    """None when the delegated clear is off in this configuration, else {"threshold": the highest `max_level` of the listed orders or None when none sets one}. Read from the same configuration the clear reads."""
    if settings(config) is None:
        return None
    ladder = (block or {}).get("ladder") or []
    levels = [e.get("max_level") for e in config[CONFIG_KEY]["orders"] if e.get("max_level") in ladder]
    return {"threshold": max(levels, key=ladder.index) if levels else None}


def safe_path(path):
    """A changed path as it may appear in a public line: the repository-relative name already public on the pull request, or a fixed phrase when it is absolute, a home path or token shaped."""
    if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9._@+/-]{1,80}", path) or path.startswith(("/", "~")) or ".." in path.split("/") \
            or re.search(r"(^|/)(Users|home)/", path) or re.search(r"[A-Za-z0-9]{24,}", path) or re.search(r"(?i)(^|[^a-z])(ghp|gho|ghu|ghs|ghr|github_pat|sk|xox[a-z])[_-]", path):
        return "a path that is not shown"
    return path


_OWNER_WORDS = {"closure": "changes a definition reachable from the clear", "machinery": "changes the clear or hold machinery", "new-file": "adds a file under a directory the daemon loads at start",
                "class": "changes a path of a class that is never delegated without a rating", "registrar": "changes a registrar in a way that touches the clear's closure"}


def _why(item):
    paths = ", ".join(safe_path(p) for p in sorted(set(item["paths"]))[:3])
    if item["kind"] == "unreadable":
        return item["why"]
    words = _OWNER_WORDS[item["kind"]]
    if item["kind"] == "new-file":
        words = words.replace("adds a file", item.get("verb") or "adds a file")
    if item["kind"] == "class":
        words = "changes a %s path that is never delegated without a rating" % item.get("cls", "protected")
    return "%s (%s)" % (words, paths) if paths else words


def who_can_clear(rule, report, files, matches, closure, block, delegation, pr=None, head=None, unread=None):
    """Who can clear this held pull request, as plain data (no text layout): {"pr", "head", "hold" (bool), "floor_ids" (the rule ids of the top tier classes it changes, sorted), "owner_only" (a list of {"kind":
    closure|new-file|machinery|class|unreadable, "why"} in words: empty means the clear does not refuse it for what it changes), "min_rating" (the matrix's lowest level for a floor hit, else None), "must_name" (the
    rule ids the rating's reason must name), "who" (one of `WHO`), "unreadable" (None or one fixed line), "delegation" ("on" or "off"), "threshold" (the highest `max_level`, or None), "needs" (what the rating or review
    at this head must still say)}. It is `refusal_items`, the clear's own rule, on the same inputs the clear reads: no second copy. Pure: it reads nothing and writes nothing. `delegation` is `delegation_of`'s answer;
    `unread` is set by a caller whose inputs could not be read at all."""
    out = {"pr": pr, "head": head, "hold": False, "floor_ids": [], "owner_only": [], "min_rating": None, "must_name": [], "who": "unknown", "unreadable": None,
           "delegation": "on" if delegation is not None else "off", "threshold": (delegation or {}).get("threshold"), "needs": []}
    if unread is not None or not isinstance(report, dict) or report.get("owner_decision") not in ("hold", "second-review", "none"):
        out["unreadable"] = UNREAD
        out["owner_only"] = [{"kind": "unreadable", "why": UNREAD}]
        return out
    out["pr"], out["head"] = report.get("pr", pr), report.get("head", head)
    if report["owner_decision"] != "hold":
        out["who"] = "no hold"
        return out
    out["hold"] = True
    scored = {"level": None, "reasons": []} if out["threshold"] else None   # with a max_level the score replaces the class refusals, as the clear does
    items, _ = refusal_items(rule, report, files, matches, closure, scored)
    unreadable = [i for i in items if i["kind"] == "unreadable"]
    ids = block["floor"]["rules"] if isinstance(block, dict) and isinstance((block.get("floor") or {}).get("rules"), dict) else None
    if unreadable or ids is None:
        out["unreadable"] = UNREAD
        out["owner_only"] = sorted(({"kind": "unreadable", "why": _why(i)} for i in unreadable), key=lambda d: d["why"]) or [{"kind": "unreadable", "why": "the score rule cannot be read"}]
        return out
    hits = report.get("top_tier_hits") or {}
    out["floor_ids"] = sorted(rid for rid, cls in ids.items() if hits.get(cls))
    out["must_name"] = list(out["floor_ids"])
    out["min_rating"] = block["floor"]["lowest_level"] if out["floor_ids"] else None
    only = {(i["kind"], _why(i)) for i in items if i["kind"] in _OWNER_WORDS}
    out["owner_only"] = [{"kind": k, "why": w} for k, w in sorted(only)]
    out["needs"] = sorted({i["text"] for i in items if i["kind"] == "review"})
    if out["floor_ids"] and out["threshold"] and not out["owner_only"] and level_refusal(block, out["min_rating"], out["threshold"]):   # no rating can pass the clear's own comparison: the floor needs more than any order allows
        need = out["min_rating"]
        out["owner_only"] = [{"kind": "score", "why": "the rating the floor needs (%s) is %s" % (need, "the owner's" if need not in block["delegable_levels"] else "above what any order allows (%s)" % out["threshold"])}]
    if out["owner_only"]:
        out["who"] = "owner only"
    elif delegation is None:
        out["who"] = "owner (delegation is off)"
    else:
        out["who"] = "chief of staff with a rating" if out["floor_ids"] and out["threshold"] else "owner or chief of staff"
    return out


def clear_line(who):
    """The ONE line the daemon posts for `who_can_clear`'s answer, or None when there is nothing to say (no hold, or unread: a read that failed never produces a confident line). It names the floor rule ids
    that hit and the lowest rating the matrix allows for them, whoever may clear."""
    if not isinstance(who, dict) or who.get("who") in (None, "no hold", "unknown"):
        return None
    head, floor = "Who can clear this: ", ""
    if who["floor_ids"]:
        floor = "; floor rules hit: %s (a rating must be at least %s and its reason must name them)" % (", ".join(who["floor_ids"]), who["min_rating"])
    if who["who"] == "owner only":
        return head + "owner only: " + "; ".join(o["why"] for o in who["owner_only"]) + floor
    if who["who"] == "owner (delegation is off)":
        return head + "the owner (delegation is off here)" + floor
    if who["floor_ids"] and who["threshold"]:
        return head + "the Chief of Staff or the owner, if the independent rating is at least %s and its reason names %s" % (who["min_rating"], " and ".join(who["floor_ids"]))
    if who["threshold"]:
        return head + "the Chief of Staff or the owner, on an independent rating of at most %s (the highest any order allows)" % who["threshold"]
    return head + "the Chief of Staff or the owner, if the independent review names no top tier class" + floor


# ---- the daemon file: which top-level definitions a changed line is in (ast, never hunk headers), and whether any of them is in the clear closure

DEF_KINDS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
BIND_KINDS = (ast.Assign, ast.AnnAssign, ast.AugAssign)


_RUNS_CODE = (ast.Call, ast.Lambda, ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def import_time(node, number):
    """True when line `number` of the definition `node` RUNS CODE WHEN THE MODULE IS IMPORTED: a decorator, a default or an annotation that calls something (a plain name or constant only
    binds, so a new function with a plain signature is not import-time code), or, in a class, anything that is not inside a method's body. A function's own body does not run at import.
    A body that starts on the line its signature ends on is judged as the signature (the stricter way)."""
    if isinstance(node, ast.ClassDef):
        for child in node.body:
            if isinstance(child, DEF_KINDS) and _span(child)[0] <= number <= child.end_lineno:
                return import_time(child, number)
        return True
    args = node.args
    header = list(node.decorator_list) + list(args.defaults) + [d for d in args.kw_defaults if d is not None] + ([node.returns] if node.returns is not None else [])
    header += [a.annotation for a in args.posonlyargs + args.args + args.kwonlyargs + [x for x in (args.vararg, args.kwarg) if x] if a.annotation is not None]
    header_end = max([node.lineno] + [x.end_lineno for x in ast.walk(args) if hasattr(x, "end_lineno")] + [h.end_lineno for h in header])
    if node.decorator_list and number < node.lineno:
        return True   # a decorator is applied (called) at import
    return number <= header_end and any(isinstance(x, _RUNS_CODE) for h in header for x in ast.walk(h))


def _span(node):
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return start, node.end_lineno


def _bound_names(node):
    names = set()
    if isinstance(node, BIND_KINDS):
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
            names |= {x.id for x in ast.walk(target) if isinstance(x, ast.Name)}
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        names |= {(a.asname or a.name).split(".")[0] for a in node.names}
    elif isinstance(node, DEF_KINDS):
        names.add(node.name)
    return names


def _index(tree):
    """({name: def node}, {global name: its binding nodes}) of the module level."""
    defs, binds = {}, {}
    for node in tree.body:
        if isinstance(node, DEF_KINDS):
            defs[node.name] = node
        else:
            for name in _bound_names(node):
                binds.setdefault(name, []).append(node)
    return defs, binds


def _is_dunder(name):
    return name.startswith("__") and name.endswith("__")


def closure(tree, roots, dynamic_actions=()):
    """(def names, global names, referenced names, {class: methods}) reachable from `roots` by NAME: every module-level definition a closure definition refers to (a call, a value, a default, a
    decorator, a base class) and every module-level binding it reads (its right-hand side, to a fixed point). A CLASS the closure refers to brings its own statements (bases, decorators, class
    body) and only those methods whose name the closure reads (as a name or an attribute) or that are special methods (`__init__`, `__getattr__`, `__call__`...): a method nothing reads is not run.
    Over-approximate by name on purpose (any attribute of that name). Dynamic dispatch by string is followed ONLY for the daemon's command table, for the actions in `dynamic_actions` (the rating's
    path, #835): the literal `"cmd_"` (the dispatcher builds `"cmd_" + action`) is an edge to `cmd_<action>` and the name `REGISTRAR_NAME_RE` (the parser collects every `add_<x>_parsers`) is an edge to
    `add_<action>_parsers`. Any OTHER dynamic edge (getattr, globals(), importlib, eval...) is a stated limit: a test lists the ones on the path from `main`."""
    defs, binds = _index(tree)
    roots = list(defs) if "*" in roots else list(roots)   # `*`: every function of the module (a module the clear runs and no recording says which)
    seen, globals_, refs, members, todo = set(), set(), set(), {}, list(roots)

    def walk(nodes):
        while nodes:
            for x in ast.walk(nodes.pop()):
                if isinstance(x, ast.Name):
                    refs.add(x.id)
                    if x.id in defs:
                        todo.append(x.id)
                    if x.id in binds and x.id not in globals_:   # a name that is a def AND is rebound (`f = wrap(f)`): both are followed
                        globals_.add(x.id)
                        nodes.extend(binds[x.id])
                elif isinstance(x, ast.Attribute):
                    refs.add(x.attr)
                elif isinstance(x, ast.Constant) and x.value == "cmd_":
                    todo.extend("cmd_" + a.replace("-", "_") for a in dynamic_actions)
                if isinstance(x, ast.Name) and x.id == "REGISTRAR_NAME_RE":
                    todo.extend("add_%s_parsers" % a.replace("-", "_") for a in dynamic_actions)

    walk([n for r in roots if r in binds and r not in globals_ and not globals_.add(r) for n in binds[r]])   # a root that is a binding is read too
    while True:
        while todo:
            name = todo.pop()
            if name in seen or name not in defs:
                continue
            seen.add(name)
            node = defs[name]
            if isinstance(node, ast.ClassDef):
                members[name] = set()
                walk([c for c in node.body if not isinstance(c, DEF_KINDS)] + list(node.bases) + list(node.keywords) + list(node.decorator_list))
            else:
                walk([node])
        grew = False
        for name in sorted(members):   # a method the closure reads by name (or a special method) is run
            for method in defs[name].body:
                if isinstance(method, DEF_KINDS) and method.name not in members[name] and (method.name in refs or _is_dunder(method.name)):
                    members[name].add(method.name)
                    walk([method])
                    grew = True
        if not grew and not todo:
            return seen, globals_, refs, members


def changed_lines(patch):
    """(old line numbers, new line numbers, lines that could not be read) of a unified diff: the REMOVED lines are numbered in the base file and the ADDED ones in the head file; a patch that is
    not text or has a hunk header it cannot read is `None`."""
    if not isinstance(patch, str):
        return None
    old, new, number_old, number_new, in_hunk = {}, {}, 0, 0, False
    for line in patch.split("\n"):
        match = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if line.startswith("@@"):
            if match is None:
                return None
            number_old, number_new, in_hunk = int(match.group(1)), int(match.group(2)), True
        elif not in_hunk or line.startswith("\\"):
            continue
        elif line.startswith("+"):
            new[number_new] = line[1:]
            number_new += 1
        elif line.startswith("-"):
            old[number_old] = line[1:]
            number_old += 1
        else:
            number_old += 1
            number_new += 1
    return old, new


def closure_refusals(base_src, head_src, patch, roots, entry_points, module_level=False, dynamic_actions=()):
    """[reasons] a change to the daemon file touches the clear closure. `base_src` and `head_src` are the file before and after, `patch` the diff of it, `roots` the clear command's entry points and
    `entry_points` the names a NEW caller of is refused. The closure is computed on BOTH files and united (a pull request that ADDS a call into it counts). The changed lines are mapped to the
    top-level definition they are in by the ast of the file they belong to (removed lines against the base, added lines against the head). Refused: a closure definition changed; a module-level
    binding or import a closure definition reads changed; any other top-level statement changed; a changed definition that refers to an entry point; a line that cannot be mapped; a syntax error."""
    lines = changed_lines(patch)
    if lines is None:
        return ["the patch of the daemon file cannot be read: refused"]
    try:
        base, head = ast.parse(base_src), ast.parse(head_src)
    except (SyntaxError, ValueError):
        return ["the daemon file does not parse before or after the change: refused"]
    base_closure, head_closure = closure(base, roots, dynamic_actions), closure(head, roots, dynamic_actions)
    in_defs, in_globals, in_refs = (base_closure[0] | head_closure[0]), (base_closure[1] | head_closure[1]), (base_closure[2] | head_closure[2])
    in_members = {k: base_closure[3].get(k, set()) | head_closure[3].get(k, set()) for k in set(base_closure[3]) | set(head_closure[3])}
    reasons = []
    for side, tree, numbers in (("base", base, lines[0]), ("head", head, lines[1])):
        spans = [(_span(n), n) for n in tree.body]
        for number, text in sorted(numbers.items()):
            owners = [n for (a, b), n in spans if a <= number <= b]
            if not owners:
                if text.strip() and not text.strip().startswith("#"):
                    reasons.append("a changed line (%s line %d) is in no top-level definition and is not a comment: it cannot be mapped" % (side, number))
                continue
            for owner in owners:
                names = _bound_names(owner)
                if isinstance(owner, DEF_KINDS):
                    method = next((c for c in owner.body if isinstance(c, DEF_KINDS) and _span(c)[0] <= number <= c.end_lineno), None) if isinstance(owner, ast.ClassDef) else None
                    unit = method or owner   # a changed line inside one method is judged by that method, not by the whole class
                    used = entry_points & {x.id for x in ast.walk(unit) if isinstance(x, ast.Name)}
                    if owner.name in in_defs and (method is None or method.name in in_members.get(owner.name, set()) or owner.name in roots):
                        reasons.append("it changes %s, part of the clear (%s file)" % (owner.name if method is None else "%s.%s" % (owner.name, method.name), side))
                    elif module_level and import_time(owner, number):
                        reasons.append("it changes import-time code of a module the clear loads (%s line %d, in %s): refused" % (side, number, owner.name))
                    elif used:
                        reasons.append("it changes %s, which refers to a clear entry point (%s)" % (owner.name, ", ".join(sorted(used)[:3])))
                elif isinstance(owner, BIND_KINDS + (ast.Import, ast.ImportFrom)):
                    if module_level:
                        reasons.append("it changes import-time code of a module the clear loads (%s line %d): refused" % (side, number))
                    elif names & (in_globals | in_defs | in_refs):
                        reasons.append("it changes the module-level binding of %s, which the clear reads" % ", ".join(sorted(names & (in_globals | in_defs | in_refs))[:3]))
                    elif len(owners) > 1:
                        reasons.append("a changed line (%s line %d) holds more than one top-level statement: refused" % (side, number))
                elif isinstance(owner, ast.Expr) and isinstance(getattr(owner, "value", None), ast.Constant):
                    continue
                else:
                    reasons.append("it changes a module-level statement (%s line %d) that is not a definition or a binding: refused" % (side, number))
    return sorted(set(reasons))


# ---- a command registrar that imports, calls or rebinds a name of the clear's closure (#964)
# In one process a hostile registrar can always undo a guard: this lint, the floor that reads it and the independent rating are the control. Both lists are DERIVED here: the closure names from `closure`
# (the one the clear uses, over the matrix's roots) and the registrars from the daemon file's own `REGISTRAR_NAME_RE`; this code holds neither, and keeps no list of ways to reach a namespace (default-deny, see `_registrar_unit`).

REGISTRAR_SOURCE_MAX = 4000000


PATTERN_NAME = "REGISTRAR_NAME_RE"


def _store_root(x):
    """True for a Store or Del of an attribute or an item whose chain starts at the pattern name (`REGISTRAR_NAME_RE.fullmatch = ...`): it changes what the name does at run time."""
    return isinstance(x, (ast.Attribute, ast.Subscript)) and isinstance(x.ctx, (ast.Store, ast.Del)) and isinstance(_chain_root(x), ast.Name) and _chain_root(x).id == PATTERN_NAME


# Every ast node class that can bind (or rebind) a name, as a predicate "this node binds PATTERN_NAME". The table is checked by a completeness test over every class of the running Python's `ast` (a class in neither this table
# nor PATTERN_NOT_BINDERS fails the test, and at run time an unlisted class is held BY DEFAULT when any string field of it is the name).
PATTERN_BINDERS = {
    "Name": lambda x: x.id == PATTERN_NAME and isinstance(x.ctx, (ast.Store, ast.Del)),   # assignment, augmented/annotated, for/with/comprehension targets, walrus, del, type alias name
    "Attribute": _store_root, "Subscript": _store_root,
    "Import": lambda x: any((a.asname or a.name).split(".")[0] == PATTERN_NAME for a in x.names),
    "ImportFrom": lambda x: any((a.asname or a.name).split(".")[0] == PATTERN_NAME for a in x.names),
    "FunctionDef": lambda x: x.name == PATTERN_NAME, "AsyncFunctionDef": lambda x: x.name == PATTERN_NAME, "ClassDef": lambda x: x.name == PATTERN_NAME,
    "ExceptHandler": lambda x: x.name == PATTERN_NAME,
    "MatchAs": lambda x: x.name == PATTERN_NAME, "MatchStar": lambda x: x.name == PATTERN_NAME, "MatchMapping": lambda x: x.rest == PATTERN_NAME,
    "Global": lambda x: PATTERN_NAME in x.names, "Nonlocal": lambda x: PATTERN_NAME in x.names,
    "arg": lambda x: x.arg == PATTERN_NAME,
    "TypeVar": lambda x: x.name == PATTERN_NAME, "ParamSpec": lambda x: x.name == PATTERN_NAME, "TypeVarTuple": lambda x: x.name == PATTERN_NAME,
}
PATTERN_NOT_BINDERS_NAMES = ("TypeAlias", "alias", "keyword", "MatchClass", "FormattedValue", "Module", "Interactive", "Expression", "FunctionType")   # no binding of their own (alias is read through its import; keyword and MatchClass name callee members)


def _binds_pattern(x):
    """True when node `x` binds, rebinds, deletes or stores through the name `REGISTRAR_NAME_RE`: by the table, and for a class of the running Python the table does not know, by default (any string field equal to the name)."""
    kind = type(x).__name__
    if kind in PATTERN_BINDERS:
        return bool(PATTERN_BINDERS[kind](x))
    if kind in PATTERN_NOT_BINDERS_NAMES or not isinstance(x, ast.AST):
        return False
    for field in getattr(x, "_fields", ()):
        value = getattr(x, field, None)
        if value == PATTERN_NAME or (isinstance(value, list) and PATTERN_NAME in value):
            return True
    return False


def pattern_bindings(tree):
    """Every node of the tree that binds the name, in source order."""
    found = [x for x in ast.walk(tree) if _binds_pattern(x)]
    return sorted(found, key=lambda x: (getattr(x, "lineno", 0), getattr(x, "col_offset", 0)))


def _literal_compile(node):
    call = node.value if isinstance(node, ast.Assign) else None
    return (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "compile" and isinstance(call.func.value, ast.Name) and call.func.value.id == "re"
            and len(call.args) >= 1 and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str))


def registrar_pattern(tree):
    """The compiled `REGISTRAR_NAME_RE` of a daemon file (read from its text, never held here), or None when the LAST binding of the name, by any form, is not a module-level `NAME = re.compile("...")`: fail closed."""
    bindings = pattern_bindings(tree)
    top = [n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == PATTERN_NAME for t in n.targets)]
    if not bindings or not top:
        return None
    last = bindings[-1]
    if last is not top[-1] and not any(y is last for y in ast.walk(top[-1].targets[0])):
        return None
    if not _literal_compile(top[-1]) or len(top[-1].targets) != 1:
        return None
    try:
        return re.compile(top[-1].value.args[0].value)
    except re.error:
        return None


def _pattern_rebound(tree, added):
    """True when a line the pull request added is a binding of `REGISTRAR_NAME_RE` of ANY form (table `PATTERN_BINDERS`) other than a lone module-level assignment, or one of several bindings: Python uses the last binding, so
    the name that selects the registrars is not changed by a line the pull request adds beside another (a class, a def, an except name, a match capture, an attribute store...)."""
    bindings = pattern_bindings(tree)
    plain = [b for b in bindings if isinstance(b, ast.Name) and any(b is t for n in tree.body if isinstance(n, ast.Assign) for t in n.targets)]
    odd = [b for b in bindings if b not in plain]
    return any(_spans(b, added) for b in odd) or (len(bindings) > 1 and any(_spans(b, added) for b in bindings))


def _chain_root(node):
    """The node an attribute or subscript chain starts from (`a` for `a.b[c].d`), whatever it is."""
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node


def _root(node):
    """The name an expression is built from: `a.b[c](d).e` is `a`; None when it starts from something else."""
    while True:
        if isinstance(node, ast.Attribute) or isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Starred):
            node = node.value
        else:
            return node.id if isinstance(node, ast.Name) else None


def _spans(node, added):
    return any(number in added for number in range(node.lineno, getattr(node, "end_lineno", node.lineno) + 1))


def _own_scope(unit):
    """([names bound in the unit's OWN scope], [names declared global]): its parameters, its stores and the names of the definitions in it, not what a nested function, lambda, class or comprehension binds (a name
    bound only in an inner scope does not hide the module's name in this one)."""
    own, glob, todo = set(), set(), [unit]
    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    while todo:
        node = todo.pop()
        if isinstance(node, ast.arg):
            own.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            own.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            glob.update(node.names)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, scopes) and child is not unit:
                if not isinstance(child, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                    own.add(child.name)
                if isinstance(child, ast.ClassDef):
                    todo.extend(child.decorator_list)
                continue
            todo.append(child)
    arguments = getattr(unit, "args", None)
    if isinstance(arguments, ast.arguments):   # the unit's own parameters
        own.update(a.arg for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs + [x for x in (arguments.vararg, arguments.kwarg) if x is not None])
    return own, glob


# ---- every way a name is bound, derived from the ast module's node classes (#964): ONE table, a test checks it against `ast` itself
# A name is bound either by a `Name` node in a Store or Del context (an assignment of any form, an augmented or annotated one, a walrus, a `for` or `with` target, a comprehension variable, a star-unpack, a `del`, a class
# body statement, a match capture of the form `case x`... all of them put a Name node there), or by a node that carries the name as a STRING: those are this table. Nothing else binds a name.
STRING_BINDERS = {
    "FunctionDef": lambda n: [n.name], "AsyncFunctionDef": lambda n: [n.name], "ClassDef": lambda n: [n.name],
    "ExceptHandler": lambda n: [n.name] if n.name else [],
    "MatchAs": lambda n: [n.name] if n.name else [], "MatchStar": lambda n: [n.name] if n.name else [], "MatchMapping": lambda n: [n.rest] if n.rest else [],
}
# the node classes whose fields carry a name as a string or an identifier and that are NOT read as a binder, with the reason (the completeness test reads `ast` and fails on a class in neither table)
NOT_BINDERS = {
    "Name": "a Name node binds only in a Store or Del context, which is read for every Name", "Attribute": "attr names a member, not a binding", "keyword": "arg names a parameter of the callee",
    "alias": "read through its Import or ImportFrom", "arg": "a parameter: the unit's own are read from its arguments, a nested function's are its own scope", "MatchClass": "kwd_attrs name attributes of a class pattern",
    "TypeAlias": "its name is a Name node in a Store context", "FormattedValue": "no name", "Module": "no name", "Interactive": "no name",
    "Import": "an import is ALWAYS a finding of its own, whatever name it binds", "ImportFrom": "an import is ALWAYS a finding of its own, whatever name it binds",
    "Global": "a global statement is ALWAYS a finding of its own", "Nonlocal": "a nonlocal statement is ALWAYS a finding of its own",
    "TypeVar": "a type parameter is a typing object scoped to its def: calling it runs nothing", "ParamSpec": "a type parameter is a typing object scoped to its def: calling it runs nothing",
    "TypeVarTuple": "a type parameter is a typing object scoped to its def: calling it runs nothing",
}


def _name_bindings(unit):
    """[(name, value, kind, node)] every binding of a name anywhere in `unit` (the unit's own name and parameters excepted): `value` is the expression the name gets (None: not an expression we can read), `kind` is "def" (an
    undecorated def or class), "lambda" (a name given a lambda) or "other". Every Name in a Store or Del context is one, so no statement form can be missed; the nodes of STRING_BINDERS are the rest."""
    given = {}
    for x in ast.walk(unit):
        pairs = []
        if isinstance(x, ast.Assign):
            pairs = [(t, x.value) for t in x.targets]
        elif isinstance(x, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)) and getattr(x, "value", None) is not None:
            pairs = [(x.target, x.value)]
        elif isinstance(x, (ast.For, ast.AsyncFor, ast.comprehension)):
            pairs = [(x.target, x.iter)]
        elif isinstance(x, (ast.With, ast.AsyncWith)):
            pairs = [(i.optional_vars, i.context_expr) for i in x.items if i.optional_vars is not None]
        for target, value in pairs:
            for y in ast.walk(target):
                if isinstance(y, ast.Name):
                    given[id(y)] = (value, "lambda" if isinstance(value, ast.Lambda) and target is y else "other")
    out = []
    own_args = {id(a) for a in ast.walk(unit.args) if isinstance(a, ast.arg)} if isinstance(getattr(unit, "args", None), ast.arguments) else set()
    for x in ast.walk(unit):
        if isinstance(x, ast.arg) and id(x) not in own_args:   # the parameter of a function or lambda nested in the unit: whatever it is called with
            out.append((x.arg, None, "param", x))
        elif isinstance(x, ast.Name) and isinstance(x.ctx, (ast.Store, ast.Del)):
            value, kind = given.get(id(x), (None, "other")) if isinstance(x.ctx, ast.Store) else (None, "other")
            out.append((x.id, value, kind, x))
        elif type(x).__name__ in STRING_BINDERS and x is not unit:
            kind = "def" if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not x.decorator_list else "other"
            out.extend((name, None, kind, x) for name in STRING_BINDERS[type(x).__name__](x))
    return out


# ---- the registrar GRAMMAR (#964, the lint is default-deny BY GRAMMAR): the only ast node classes a registrar (and a helper it is read with) may contain. The table is MEASURED from the real registrars (every `add_<x>_parsers` of the
# daemon file and of the registrar split, and the helper they call) by a test, which fails when the real code uses a class that is not here and when a class here is used by no real code and has no reason; a class that is not in the
# table is held BY NAME, and so is a node class of a future Python (a test fails on an `ast` class that is in none of GRAMMAR, GRAMMAR_REASONS and the classes the grammar does not need to name).
REGISTRAR_GRAMMAR = {
    # statements
    "FunctionDef": "a function the registrar defines itself (`def common(p, ...)`): its body is read with it",
    "Assign": "`thing = sub.add_parser(...)`, `x, y = a, b`: every target and value is read by the binding rules",
    "Expr": "`thing.add_argument(...)`: a call as a statement",
    "For": "`for p in (a, b):`, `for action in parser._actions:`: a fresh target over an own parser or a literal (the binding rules hold anything else)",
    "If": "`if x:` around flags: its test is read like any expression",
    # expressions
    "Call": "a call is read by the call rule (a method of an own parser, a defined function, a table name bound once)", "Attribute": "`thing.add_argument`, `argparse.SUPPRESS`: read by the use and store rules",
    "Name": "read by the use, binding and call rules", "Constant": "a literal", "Tuple": "a tuple of values", "Dict": "a dict of keyword values (`**given`): its values are read like any argument",
    "BinOp": "`'--' + flag`, `'%d' % n`", "IfExp": "`a if c else b`", "Compare": "`p is a`, `x == y`", "Subscript": "a load (`names[0]`): a store through one is held by the store rule", "keyword": "`type=int`: read by the argument rule",
    # the structure of a function and of the contexts and operators the classes above carry
    "arguments": "the parameters of a function", "arg": "a parameter: read as a binding", "Load": "an expression context", "Store": "a target context",
    "Add": "an operator of BinOp", "Mod": "an operator of BinOp", "Eq": "an operator of Compare", "Is": "an operator of Compare",
}
# the node classes whose presence says nothing by itself and that the grammar need not name: the module root and the operators and contexts of classes the grammar already allows or that it holds anyway
GRAMMAR_NEUTRAL = {"FormattedValue", "Module", "Interactive", "Expression", "FunctionType", "Del", "Param", "AugLoad", "AugStore", "Sub", "Mult", "Div", "FloorDiv", "Pow", "LShift", "RShift", "BitOr", "BitXor", "BitAnd", "MatMult",
                   "And", "Or", "Not", "Invert", "UAdd", "USub", "NotEq", "Lt", "LtE", "Gt", "GtE", "IsNot", "In", "NotIn"}


def _registrar_unit(unit, names, added, helpers=None, opaque=(), literals=(), readable=(), builtins=(), named_helpers=(), verbs=(), free_keywords=()):
    """[(verb, name)] a registrar (a function, a class, or the lambda a registrar name is assigned) does that a registrar may not, counting only the nodes that touch a line in `added` (`None`: every line). It is
    DEFAULT-DENY: a registrar sets up its own parsers and nothing else, so in it is a finding (named by the closure name when it touches one, else by what it touches): an import; a call of anything that is not a method of its
    own parser objects or a function it defines itself; a store into, or delete of, an attribute or an item of anything that is not its own parser object, or a `global` statement; a reach into a namespace by attribute
    (a dunder, a frame's `f_` fields); and a closure name loaded anywhere but as a value handed to its own parser (`type=issue_arg`). No list of ways to reach another module is kept, so none can fall short. `names` are
    the closure names; `helpers`, when a set, collects the names the unit loads that are not closure names and not its own (a helper it may call: the caller reads those too); a call of a name in `opaque` (a
    module-level binding that cannot be read) is a finding; `literals` are the closure names bound only to an immutable literal (data, not a closure name for the use rule)."""
    own, glob = _own_scope(unit)
    own -= glob   # a name declared `global` is the module's, whatever is stored into it
    live = set(names) - set(literals) - set(named_helpers)   # a named registration helper (the allow table) is pure by its own test: using it runs nothing of the closure
    if helpers is not None:
        helpers.update(x.id for x in ast.walk(unit) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id not in own and x.id not in names)
    arguments = getattr(unit, "args", None)
    handle, built, data, param_defaults = set(), set(), set(), []   # its own parser objects: what it is handed, and what it builds from them (a parameter with a literal default is data, one with any other default is a finding)
    if isinstance(arguments, ast.arguments):
        positional = arguments.posonlyargs + arguments.args
        defaults = dict(zip([a.arg for a in positional[len(positional) - len(arguments.defaults):]], arguments.defaults))
        defaults.update({a.arg: d for a, d in zip(arguments.kwonlyargs, arguments.kw_defaults) if d is not None})
        for a in positional + arguments.kwonlyargs:
            if a.arg not in defaults:
                handle.add(a.arg)   # the handle it is given: shared with every other registrar, so only the verbs of the allow table may be used on it
            elif _is_data(defaults[a.arg], set()):
                data.add(a.arg)
            else:
                param_defaults.append((a, defaults[a.arg]))
    def verb_chain(expr):
        """True when `expr` is a call chain made only of the allow table's parser methods and rooted at the handle or a parser it built: `sub.add_parser("x")`, `t.add_argument(...)`, `sub.add_parser("x").add_argument(...)`."""
        if not isinstance(expr, ast.Call):
            return False
        func = expr.func
        if not (isinstance(func, ast.Attribute) and func.attr in verbs):
            return False
        inner = func.value
        return (isinstance(inner, ast.Name) and inner.id in handle | built) or verb_chain(inner)
    grew = True   # names that hold a literal (a string, a tuple of strings) or an element of one: calling `.replace` on them runs nothing
    while grew:
        before, grew = len(data) + len(built), False
        for x in ast.walk(unit):
            sources = []
            if isinstance(x, ast.Assign) and verb_chain(x.value):
                sources = x.targets   # `t = sub.add_parser(...)`: an object the registrar CREATED
            elif isinstance(x, (ast.For, ast.comprehension)) and ((isinstance(x.iter, (ast.Tuple, ast.List)) and x.iter.elts and all(isinstance(e, ast.Name) and e.id in built for e in x.iter.elts))
                                                                  or (isinstance(x.iter, ast.Attribute) and isinstance(x.iter.value, ast.Name) and x.iter.value.id in built)):
                sources = [x.target]   # `for p in (a_built_parser, another):`, `for action in a_built_parser._actions:`
            for t in sources:
                for y in ast.walk(t):
                    if isinstance(y, ast.Name) and y.id not in built:
                        built.add(y.id)
            if isinstance(x, ast.Assign) and _is_data(x.value, data | set(literals)):
                data.update(y.id for t in x.targets for y in ast.walk(t) if isinstance(y, ast.Name))
            elif isinstance(x, (ast.For, ast.comprehension)) and _is_data(x.iter, data | set(literals)):
                data.update(y.id for y in ast.walk(x.target) if isinstance(y, ast.Name))
        if len(data) + len(built) != before:
            grew = True
    bindings, kinds, records = {}, {}, _name_bindings(unit)   # every binding of every name in the unit, with the value it gets (None: a value that is not data) and how it is bound: a name is data only if EVERY binding of it is data, and a function the registrar DEFINES only if every binding of it is a def or a lambda
    for name, value, kind, _node in records:
        bindings.setdefault(name, []).append(value)
        kinds.setdefault(name, []).append(kind)
    if isinstance(arguments, ast.arguments):   # the unit's OWN parameters are bindings too (a parameter named `len` is the handle, not the builtin)
        for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs + [x for x in (arguments.vararg, arguments.kwarg) if x is not None]:
            kinds.setdefault(a.arg, []).append("param")
    if isinstance(arguments, ast.arguments):
        for a, _unread in param_defaults:
            bindings.setdefault(a.arg, []).append(None)
        for a in positional + arguments.kwonlyargs:
            if a.arg in defaults and a.arg in data:
                bindings.setdefault(a.arg, []).append(defaults[a.arg])
    settled = False
    while not settled:
        settled = True
        for n in sorted(data):
            if any(v is None or not _is_data(v, (data - {n}) | set(literals)) for v in bindings.get(n, [])):
                data.discard(n)
                settled = False
    parser = handle | built
    defined = {n for n, k in kinds.items() if len(k) == 1 and k[0] in ("def", "lambda") and n not in handle and n not in built}   # a function the registrar defines itself (its body is read with it), and never rebinds to anything else
    def names_aliases(expr, seen=()):
        """True when a `**` expansion may carry an `aliases` key: a dict with that key, a conditional of such, or a name bound to one (an expansion that is anything else cannot be read, and `unsafe` holds it)."""
        if isinstance(expr, ast.Dict):
            return any(isinstance(k, ast.Constant) and k.value == "aliases" for k in expr.keys) or any(k is None for k in expr.keys)
        if isinstance(expr, ast.IfExp):
            return names_aliases(expr.body, seen) or names_aliases(expr.orelse, seen)
        if isinstance(expr, ast.Name) and expr.id not in seen:
            return any(v is not None and names_aliases(v, seen + (expr.id,)) for v in bindings.get(expr.id, []))
        return False
    resolving = set()
    def unsafe(expr):
        """None when `expr` is a value argparse may hold and, at parse time, run or apply without running anything of the closure: a literal, a container of them, a name that holds data, a builtin or helper of the allow table,
        a function the registrar defines or can be read, a parser object, an upper-case constant of a module, a lambda (its body is read with the unit), or an expression of those. Else the name that is not."""
        if isinstance(expr, ast.Constant):
            return None
        if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
            return next((u for u in map(unsafe, expr.elts) if u), None)
        if isinstance(expr, ast.Dict):
            return next((u for u in map(unsafe, [k for k in expr.keys if k is not None] + expr.values) if u), None)
        if isinstance(expr, ast.Starred):
            return unsafe(expr.value)
        if isinstance(expr, ast.Lambda):
            return None
        if isinstance(expr, ast.Name):
            if expr.id in bindings and expr.id not in handle and expr.id not in built and expr.id not in resolving and bindings[expr.id] and all(v is not None for v in bindings[expr.id]):
                resolving.add(expr.id)   # a local that is only ever given safe values (`given = {...} if suppress else {}`) is safe
                try:
                    if all(unsafe(v) is None for v in bindings[expr.id]):
                        return None
                finally:
                    resolving.discard(expr.id)
            ok = expr.id in data or expr.id in literals or expr.id in parser or (expr.id in named_helpers and expr.id not in kinds) or expr.id in defined or (expr.id in readable and expr.id not in kinds) or (expr.id in builtins and expr.id not in kinds)
            return None if ok else expr.id
        if isinstance(expr, ast.Attribute):
            return None if (expr.attr.isupper() and isinstance(expr.value, ast.Name)) else (_chain_root(expr).id if isinstance(_chain_root(expr), ast.Name) else expr.attr)
        if isinstance(expr, ast.Call):
            callee = expr.func
            if isinstance(callee, ast.Name) and (callee.id in builtins or callee.id in named_helpers) and callee.id not in kinds:
                return next((u for u in map(unsafe, list(expr.args) + [k.value for k in expr.keywords]) if u), None)
            if isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name) and (callee.value.id in data or callee.value.id in literals) and callee.value.id not in own - data:   # a method of a string or tuple of data (`flag.replace("_", "-")`)
                return next((u for u in map(unsafe, list(expr.args) + [k.value for k in expr.keywords]) if u), None)
            return callee.id if isinstance(callee, ast.Name) else "a call"
        children = [c for c in ast.iter_child_nodes(expr) if isinstance(c, ast.expr)]
        if isinstance(expr, (ast.BinOp, ast.BoolOp, ast.UnaryOp, ast.IfExp, ast.Compare, ast.JoinedStr, ast.FormattedValue)):
            return next((u for u in map(unsafe, children) if u), None)
        return "a value argparse may run"
    found = set()
    def note(node, verb, name):
        if added is None or _spans(node, added):
            found.add((verb, name))
    def label(*candidates):
        for c in candidates:
            if c in names:
                return c
        return next((c for c in candidates if isinstance(c, str) and c), "a name that cannot be read")
    for a, default in param_defaults:   # `def register(parser, m=sys.modules["__main__"])`: a default that runs or reaches something is not the registrar's own
        note(a, "rebinds", a.arg)
    for name, value, kind, node in records:   # a parser name bound in ANY form to anything that is not built from a parser (a `del`, an `except as`, an import, a `global`, a match capture, a `with` or `for` target...) is a finding
        if name not in parser or kind == "param":   # (a nested function's parameter is that function's own name, not a rebinding of the registrar's)
            continue
        ok = value is not None and (verb_chain(value) or _is_data(value, data | set(literals)) or (isinstance(node, ast.Name) and _built_iter(value, built)))
        if not ok:
            note(node, "rebinds", name)
    for x in ast.walk(unit):   # a decorator, a base class or a class keyword runs when the definition does: it must be a value that runs nothing of the closure
        # IMPLICIT CALLS (Python calls something here with no Call node): a decorator is a callee applied to the object (the unit's own decorators included), a base class or class keyword is called when the class is made,
        # a `with` item runs __enter__ and __exit__, a `for` iterable runs __iter__: each must be a value that runs nothing of the closure (a name of data, the table or a parser the registrar built), else it is held
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for expr in list(x.decorator_list) + (list(x.bases) + [k.value for k in x.keywords] if isinstance(x, ast.ClassDef) else []):
                bad = unsafe(expr) if not isinstance(expr, ast.Call) else (unsafe(expr.func) or "a call")
                if bad:
                    note(x, "calls", label(bad))
        elif isinstance(x, (ast.With, ast.AsyncWith)):
            for item in x.items:
                bad = unsafe(item.context_expr) if not isinstance(item.context_expr, ast.Call) else None
                if bad:
                    note(x, "calls", label(bad))
        elif isinstance(x, (ast.For, ast.AsyncFor, ast.comprehension)) and not _built_iter(x.iter, built):
            bad = unsafe(x.iter) if not isinstance(x.iter, ast.Call) else None
            if bad:
                note(x.iter, "calls", label(bad))
    allowed, callee_roots = set(), set()
    for x in ast.walk(unit):
        if isinstance(x, ast.Call):
            node = x.func
            while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
                node = node.value if not isinstance(node, ast.Call) else node.func
            if isinstance(node, ast.Name):
                callee_roots.add(id(node))
            if verb_chain(x):
                for arg in list(x.args) + [k.value for k in x.keywords]:
                    allowed.update(id(y) for y in ast.walk(arg) if isinstance(y, ast.Name))
        elif isinstance(x, ast.Attribute) and isinstance(x.ctx, ast.Load) and x.attr.isupper() and isinstance(x.value, ast.Name):
            allowed.add(id(x.value))   # a constant read (`argparse.SUPPRESS`): nothing runs
    literal_stores = {id(t) for x in ast.walk(unit) if isinstance(x, ast.Assign) and _is_data(x.value, data | set(literals)) for target in x.targets for t in ast.walk(target)
                      if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id in built}
    for x in ast.walk(unit):
        if isinstance(x, ast.Attribute) and isinstance(x.ctx, ast.Load) and x.attr not in verbs and isinstance(_chain_root(x), ast.Name) and _chain_root(x).id in handle and not x.attr.isupper():
            note(x, "uses", x.attr)   # a member of the handle that is not one of the allow table's methods (`sub.choices`, `sub._actions`): it reaches the other commands' parsers
    for x in ast.walk(unit):
        if isinstance(x, (ast.Import, ast.ImportFrom)):
            bound = [a.name.split(".")[0] for a in x.names] + [a.asname for a in x.names if a.asname] + ([x.module.split(".")[0]] if isinstance(x, ast.ImportFrom) and x.module else [])
            note(x, "imports", label(*bound))
        elif isinstance(x, (ast.Global, ast.Nonlocal)):
            note(x, "rebinds", label(*x.names))
        elif isinstance(x, ast.Call):
            callee = _root(x.func)
            fname = x.func.id if isinstance(x.func, ast.Name) else (x.func.attr if isinstance(x.func, ast.Attribute) else None)
            if any(k.arg == "aliases" or (k.arg is None and names_aliases(k.value)) for k in x.keywords):
                note(x, "calls", "aliases")   # `add_parser(..., aliases=[...])` or `**{"aliases": [...]}` names another command
            if fname == "set_defaults" and isinstance(x.func, ast.Attribute) and isinstance(x.func.value, ast.Name) and x.func.value.id in handle:
                note(x, "calls", "set_defaults on the handed parser")   # it sets defaults every command shares
            if verb_chain(x):   # what argparse may RUN at parse time (`type=`, `action=`, a default it converts, `choices=`, ...): only values that run nothing of the closure; the free keywords hold text or a stored function
                for value, key in [(a, None) for a in x.args] + [(k.value, k.arg) for k in x.keywords]:
                    if key in free_keywords:
                        continue
                    if key == "action" and not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                        note(x, "calls", label(unsafe(value) or "action"))   # `action=` takes a string literal only
                        continue
                    bad = unsafe(value)
                    if bad:
                        note(x, "calls", label(bad))
            if isinstance(x.func, ast.Name) and (x.func.id in builtins or x.func.id in named_helpers) and x.func.id not in kinds:   # a call of the table: each argument is run through the same value check (`min(..., key=exit)` runs `exit`)
                for arg in list(x.args) + [k.value for k in x.keywords]:
                    bad = unsafe(arg)
                    if bad:
                        note(x, "calls", label(bad))
            if fname in ("__import__", "import_module"):
                note(x, "imports", label(callee))
            elif verb_chain(x) or callee in data or (isinstance(x.func, ast.Name) and (x.func.id in defined or (x.func.id in (set(readable) | set(named_helpers) | set(builtins)) and x.func.id not in kinds))) or (isinstance(x.func, ast.Attribute) and _root(x.func) is None and isinstance(x.func.value, ast.Constant)):
                pass   # a method of its own parser, a function it defines, a method of a literal (`"..." .format`)
            else:
                note(x, "calls", label(callee, fname))
            if isinstance(x.func, ast.Name) and x.func.id in opaque and x.func.id not in own and x.func.id not in names:
                note(x, "calls", "a helper that cannot be read (%s)" % x.func.id)
        elif isinstance(x, (ast.Attribute, ast.Subscript)) and isinstance(x.ctx, (ast.Store, ast.Del)):
            if id(x) not in literal_stores:   # a plain store of a literal on a parser the registrar built (`action.default = None`); every other store is held, the handle's included
                key = x.slice if isinstance(x, ast.Subscript) else None
                note(x, "rebinds", label(getattr(x, "attr", None), key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None, _root(x.value)))
        if isinstance(x, ast.Attribute) and (x.attr.startswith("__") and x.attr.endswith("__") or x.attr.startswith("f_")):
            note(x, "rebinds", label(x.attr, _root(x.value)))   # a dunder or a frame field reaches a namespace: what it can write cannot be read
        elif isinstance(x, ast.Name) and x.id in ("__builtins__", "__import__", "__loader__", "__spec__", "__dict__") :
            note(x, "rebinds", label(x.id))
        if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in live and x.id not in own and (id(x) not in allowed or id(x) in callee_roots):   # THE USE RULE
            note(x, "uses", x.id)
    parents = {id(c): n for n in ast.walk(unit) for c in ast.iter_child_nodes(n)}   # THE GRAMMAR: any node class outside the table is held by name (a node with no line is reported at its nearest ancestor that has one)
    for x in ast.walk(unit):
        kind = type(x).__name__
        if x is unit and isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) or kind in REGISTRAR_GRAMMAR or kind in GRAMMAR_NEUTRAL:
            continue
        where = x
        while not hasattr(where, "lineno") and id(where) in parents:
            where = parents[id(where)]
        note(where, "uses", "the statement or expression form %s, which a registrar may not contain" % kind)
    return sorted(found)


def _built_iter(expr, built):
    """True for the iterable of a loop whose variable is one of the registrar's own parsers: a tuple of names of built parsers, or an attribute of one (`for action in built._actions`)."""
    return (isinstance(expr, (ast.Tuple, ast.List)) and bool(expr.elts) and all(isinstance(e, ast.Name) and e.id in built for e in expr.elts)) \
        or (isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) and expr.value.id in built)


def _is_data(expr, data):
    """True for an expression that is an IMMUTABLE literal or built only from them and names that hold them: a string, a number, a tuple of them, `"a" + b`. A list, a set or a dict is not data: it can be appended to
    and then holds whatever was put in it."""
    if isinstance(expr, ast.Constant):
        return True
    if isinstance(expr, ast.Name):
        return expr.id in data
    if isinstance(expr, ast.Tuple):
        return all(_is_data(e, data) for e in expr.elts)
    if isinstance(expr, ast.BinOp):
        return _is_data(expr.left, data) and _is_data(expr.right, data)
    return False


def _is_const_expr(expr, data):
    """True for an immutable constant expression of the module: a constant, a tuple of them, a name that is one, `A + B` or `A % B` of them, an upper-case attribute of a name (a module's constant)."""
    if isinstance(expr, ast.Constant):
        return True
    if isinstance(expr, ast.Name):
        return expr.id in data
    if isinstance(expr, ast.Tuple):
        return all(_is_const_expr(e, data) for e in expr.elts)
    if isinstance(expr, ast.BinOp):
        return _is_const_expr(expr.left, data) and _is_const_expr(expr.right, data)
    if isinstance(expr, ast.Attribute):
        return expr.attr.isupper() and isinstance(expr.value, ast.Name)
    return False


def _immutable(value):
    """True for a str, bytes, number, bool, None, or a tuple or frozenset of those: data a registrar cannot use to run anything and nothing can append to."""
    if isinstance(value, (str, bytes, int, float, complex, bool)) or value is None:
        return True
    return isinstance(value, (tuple, frozenset)) and all(_immutable(v) for v in value)


def _module_statements(body):
    """Every statement the module runs by itself: the body, and the bodies of an `if`, `try`, `with`, `for` or `while` in it; never the inside of a def or a class."""
    for node in body:
        yield node
        if isinstance(node, ast.Match):
            for case in node.cases:
                yield from _module_statements(case.body)
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, getattr(ast, "TryStar", ast.Try))):
            for field in ("body", "orelse", "finalbody"):
                yield from _module_statements(getattr(node, field, []))
            for handler in getattr(node, "handlers", []):
                yield from _module_statements(handler.body)


def _spans_from_decorators(node, numbers):
    """True when a line in `numbers` is in the statement, from its first decorator to its last line."""
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return any(n in numbers for n in range(start, getattr(node, "end_lineno", node.lineno) + 1))


# the names by which Python reaches a module's namespace from a statement; a module-level statement that names one is held (the bare-expression rule above is the default-deny, this covers the same reach inside an assignment)
NAMESPACE_NAMES = {"globals", "vars", "locals", "setattr", "delattr", "getattr", "exec", "eval", "compile", "__import__", "import_module", "__dict__", "modules", "__builtins__", "builtins"}


NAMESPACE_ATTRIBUTE_EXEMPT = {"compile"}   # `re.compile(...)` is an attribute named like the builtin and reaches no namespace; the BARE builtin `compile(...)` is still held


def _import_time_statements(body):
    """Every statement that RUNS when the module is imported: the module's own statements, those under an `if`, `try`, `with`, `for`, `while` or `match`, and the statements of a CLASS BODY (a class body runs at import time, and so does a class nested in it). Never the body of a def."""
    for node in body:
        yield node
        if isinstance(node, ast.ClassDef):
            yield from _import_time_statements(node.body)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                yield from _import_time_statements(case.body)
        elif isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, getattr(ast, "TryStar", ast.Try))):
            for field in ("body", "orelse", "finalbody"):
                yield from _import_time_statements(getattr(node, field, []))
            for handler in getattr(node, "handlers", []):
                yield from _import_time_statements(handler.body)


def _shallow_nodes(node):
    """The nodes of a statement that run when the statement runs: its own expressions, a def's or class's header (decorators, defaults, bases), and never the body of a def, a class or a lambda, nor a nested statement (those are yielded on their own)."""
    todo = [node]
    while todo:
        x = todo.pop()
        yield x
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            header = list(getattr(x, "decorator_list", [])) + list(getattr(x, "bases", [])) + [k.value for k in getattr(x, "keywords", [])]
            arguments = getattr(x, "args", None)
            if isinstance(arguments, ast.arguments):
                header += list(arguments.defaults) + [d for d in arguments.kw_defaults if d is not None]
            todo.extend(header)
            continue
        todo.extend(c for c in ast.iter_child_nodes(x) if not isinstance(c, ast.stmt))


def _bound_here(node):
    """The names a module-level statement binds BY ITSELF, derived from the same forms as `_name_bindings` (every Name in a Store or Del context, the nodes of STRING_BINDERS, imports and `global` statements): an assignment of any
    form, a walrus or a match capture anywhere in the statement's own expressions or patterns, a loop or `with` target, an `except` name, a def, a class. Statements nested in it are NOT read here (`_module_statements` yields them
    on their own), and nothing inside a def, a class or a lambda is (their locals are their own)."""
    out, todo = [], [node]
    while todo:
        x = todo.pop()
        if isinstance(x, ast.Name) and isinstance(x.ctx, (ast.Store, ast.Del)):
            out.append(x.id)
        elif type(x).__name__ in STRING_BINDERS:
            out.extend(STRING_BINDERS[type(x).__name__](x))
        elif isinstance(x, (ast.Import, ast.ImportFrom)):
            out.extend(a.asname or a.name.split(".")[0] for a in x.names)
        elif isinstance(x, (ast.Global, ast.Nonlocal)):
            out.extend(x.names)
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            header = list(getattr(x, "decorator_list", [])) + list(getattr(x, "bases", [])) + [k.value for k in getattr(x, "keywords", [])]   # all of these run in the ENCLOSING scope (a walrus in one binds a module name)
            arguments = getattr(x, "args", None)
            if isinstance(arguments, ast.arguments):
                header += list(arguments.defaults) + [d for d in arguments.kw_defaults if d is not None]
                header += [a.annotation for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs + [v for v in (arguments.vararg, arguments.kwarg) if v is not None] if a.annotation is not None]
            if getattr(x, "returns", None) is not None:
                header.append(x.returns)
            todo.extend(header)
            continue
        todo.extend(c for c in ast.iter_child_nodes(x) if not isinstance(c, ast.stmt))
    return out


def _binds_registrar(node, patterns):
    """[(name, unit)] the registrars a module-level statement defines or binds: a def, an async def, a class, an assignment (plain, annotated, augmented, tuple or multi target) whose value is a lambda or anything else
    (then the unit is None: it cannot be read), an import under a registrar name. Whatever `command_registrars` could pick up by name out of the module's globals."""
    def named(name):
        return isinstance(name, str) and any(p.fullmatch(name) for p in patterns)
    extra = [(n, None) for n in _bound_here(node) if named(n)]   # every OTHER way a registrar name can be bound (a walrus, a loop or with target, an except name, a match capture): not readable, so held
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return ([(node.name, node)] if named(node.name) else []) + [e for e in extra if e[0] != node.name]
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        out = []
        for target in targets:
            for y in ast.walk(target):
                if isinstance(y, ast.Name) and named(y.id):
                    value = getattr(node, "value", None)
                    out.append((y.id, value if isinstance(value, ast.Lambda) and isinstance(target, ast.Name) else None))
        return out + [e for e in extra if e[0] not in {o[0] for o in out}]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name.split(".")[0], None) for a in node.names if named(a.asname or a.name.split(".")[0])]
    return extra


class Findings(list):
    """The findings of `registrar_findings`: a plain list of (registrar, verb, name) that also carries `closure`, the closure names the lint derived (so a reason can say which name the closure derivation names)."""
    closure = frozenset()


def registrar_findings(base_src, head_src, rule):
    """([(registrar, verb, closure name)], None) for a pull request's own base and head text of the daemon file, or (None, why) when they cannot be read (the caller holds, fail closed). A finding is an
    import, a call or a rebinding of a closure name by a registrar on a line the pull request added; an untouched line or a removed one gives none. A registrar is whatever binds a name the daemon file's own
    `REGISTRAR_NAME_RE` matches, wherever the module binds it (def, async def, class, assignment of any form, import, an `if` or `try` around it, a write into `globals()`); a registrar that cannot be read is held. A
    function a registrar refers to that is not a closure name (a helper) is read as part of the registrar (every line of it when a changed line of the registrar names it). The closure is the union of the base and
    the head (a pull request that ADDS a closure name counts); the registrar pattern is the union of both files' own `REGISTRAR_NAME_RE`."""
    if not isinstance(base_src, str) or not isinstance(head_src, str) or len(base_src) > REGISTRAR_SOURCE_MAX or len(head_src) > REGISTRAR_SOURCE_MAX:
        return None, "the daemon file before or after the change cannot be read"
    try:
        base, head = ast.parse(base_src), ast.parse(head_src)
        roots, actions = daemon_roots(rule), rule.get("dynamic_actions") or ()
    except (SyntaxError, ValueError, KeyError, TypeError, AttributeError, RecursionError, MemoryError):
        return None, "the daemon file does not parse before or after the change, or the rule cannot be read"
    patterns = [p for p in (registrar_pattern(base), registrar_pattern(head)) if p is not None]
    if not patterns or registrar_pattern(head) is None:
        return None, "the registrar name pattern of the daemon file cannot be read"
    names = set()
    for tree in (base, head):
        reach = closure(tree, roots, actions)
        names |= reach[0] | reach[1]
    if not names:
        return None, "the clear's closure is empty, so it cannot be told what a registrar touches"
    patch = "\n".join(difflib.unified_diff(base_src.split("\n"), head_src.split("\n"), n=0, lineterm=""))
    lines = changed_lines(patch)
    if lines is None:
        return None, "the diff of the daemon file cannot be read"
    added = set(lines[1])
    if _pattern_rebound(head, added):
        return None, "a changed line binds the registrar name pattern of the daemon file"
    allow = rule.get("registrar_allow") if isinstance(rule, dict) else None
    if allow is not None and not (isinstance(allow, dict) and all(isinstance(allow.get(k, []), list) and all(isinstance(v, str) for v in allow.get(k, [])) for k in ("pure_builtins", "helpers", "parser_methods", "free_keywords"))):
        return None, "the registrar allow table of the rule cannot be read"
    pure_builtins, named, verbs, free = set((allow or {}).get("pure_builtins", [])), set((allow or {}).get("helpers", [])), set((allow or {}).get("parser_methods", [])), set((allow or {}).get("free_keywords", []))
    rebound = {b for node in _module_statements(head.body) for b in _bound_here(node)} & pure_builtins
    if rebound:   # the module rebinds a builtin of the table: it is no longer that builtin, so the table does not allow it (a registrar's call of it is held)
        pure_builtins = pure_builtins - rebound
    found, defs, opaque = [], {}, set()   # the helpers: what the module binds that is not the closure; `opaque` are the names that cannot be read as ONE def or lambda
    head_bindings = {}   # every module-level binding of every name in the head file, from the one derivation (`_bound_here`), with how it binds: a def, a lambda, or other
    for node in _module_statements(head.body):
        simple = {}
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.decorator_list:
            simple[node.name] = ("def", node)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Lambda):
            simple[node.targets[0].id] = ("lambda", node.value)
        for bound in _bound_here(node):
            head_bindings.setdefault(bound, []).append(simple.get(bound, ("other", None)))
            if bound in simple:
                simple.pop(bound)
    for bound, kinds_ in head_bindings.items():
        if bound in names or bound in named:
            continue
        if len(kinds_) == 1 and kinds_[0][0] in ("def", "lambda"):
            unit = kinds_[0][1]
            if kinds_[0][0] == "lambda":
                owner = next((n for n in _module_statements(head.body) if isinstance(n, ast.Assign) and n.value is unit), None)
                if owner is None:
                    return None, "a function bound to a module name cannot be traced to its assignment"
                unit.lineno, unit.end_lineno = owner.lineno, owner.end_lineno
            defs[bound] = unit
        else:
            opaque.add(bound)   # bound twice, decorated, imported, a loop or with target, a walrus, an augmented assignment, a match capture...: it cannot be read as one function
    module_values = {}   # every module-level binding of every name, with its value (None: not an expression we can read as data)
    for tree in (base, head):
        for node in _module_statements(tree.body):
            valued = set()
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and getattr(node, "value", None) is not None:
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    for y in ast.walk(target):
                        if isinstance(y, ast.Name):
                            module_values.setdefault(y.id, []).append(node.value if isinstance(target, ast.Name) else None)
                            valued.add(y.id)
            for bound in _bound_here(node):   # everything else the statement binds (a walrus in its value, a def, an import, a loop or with target, an augmented assignment...): not data
                if bound not in valued:
                    module_values.setdefault(bound, []).append(None)
    literals = set(module_values)   # data: every module-level name whose bindings are ALL immutable constant expressions (a literal, a tuple, `A + B` of data, a module's upper-case constant)
    settled = False
    while not settled:
        settled = True
        for n in sorted(literals):
            if any(v is None or not _is_const_expr(v, literals - {n}) for v in module_values[n]):
                literals.discard(n)
                settled = False
    # an upper-case name the file never binds anywhere (not a store, def, class, parameter or import) is a constant the daemon's loader puts in the module's namespace from another module (`ORDERS_HEADING`): data, as an
    # upper-case attribute of a module is. A closure name is never data by this rule; and a CALL of it is still held (a call needs exactly one binding of its own).
    bound_anywhere = set(module_values)
    for x in ast.walk(head):
        if isinstance(x, ast.Name) and not isinstance(x.ctx, ast.Load):
            bound_anywhere.add(x.id)
        elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound_anywhere.add(x.name)
        elif isinstance(x, ast.arg):
            bound_anywhere.add(x.arg)
        elif isinstance(x, ast.alias):
            bound_anywhere.add((x.asname or x.name).split(".")[0])
        elif isinstance(x, (ast.Global, ast.Nonlocal)):
            bound_anywhere.update(x.names)
        elif isinstance(x, ast.ExceptHandler) and x.name:
            bound_anywhere.add(x.name)
    literals |= {x.id for x in ast.walk(head) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id.isupper() and x.id not in bound_anywhere and x.id not in names}
    def read(label, unit, lines_counted):
        helpers = set()
        for verb, hit in _registrar_unit(unit, names, lines_counted, helpers, opaque, literals, set(defs), pure_builtins, named, verbs, free):
            found.append((label, verb, hit))
        return helpers
    for node in _module_statements(head.body):
        for name, unit in _binds_registrar(node, patterns):
            if unit is None:
                if _spans(node, added):
                    found.append((name, "rebinds", "a registrar bound in a form that cannot be read"))
                continue
            if isinstance(unit, ast.Lambda):
                unit.lineno, unit.end_lineno = node.lineno, node.end_lineno
            done, todo = {}, [(name, unit, added)]
            while todo:   # the registrar, then the helpers it refers to (all lines of a helper that a changed line of its caller names)
                label, current, counted = todo.pop()
                refs = read(name if label == name else "%s (through %s)" % (name, label), current, counted)
                for ref in sorted(refs & set(defs)):
                    full = counted is None or any(isinstance(n, ast.Name) and n.id == ref and _spans(n, added) for n in ast.walk(current))
                    if ref not in done or (full and done[ref] is not None):
                        done[ref] = None if full else added
                        todo.append((ref, defs[ref], done[ref]))
    def reaches(y):   # a node that writes or reaches a namespace: a global statement, a name or attribute of NAMESPACE_NAMES, a frame field or a dunder attribute
        return isinstance(y, (ast.Global, ast.Nonlocal)) or (isinstance(y, ast.Name) and y.id in NAMESPACE_NAMES) \
            or (isinstance(y, ast.Attribute) and ((y.attr in NAMESPACE_NAMES and y.attr not in NAMESPACE_ATTRIBUTE_EXEMPT) or y.attr.startswith("f_") or (y.attr.startswith("__") and y.attr.endswith("__"))))
    writer_nodes = [n for n in ast.walk(head) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and any(reaches(y) for y in ast.walk(n))]   # ANY function, lambda or method anywhere in the file that writes or reaches a namespace
    writers = {n.name for n in writer_nodes if hasattr(n, "name")}
    for owner in ast.walk(head):   # a lambda assigned to a name, and a method of a class: the names they are called by
        if isinstance(owner, ast.Assign) and isinstance(owner.value, ast.Lambda) and owner.value in writer_nodes:
            writers.update(t.id for t in owner.targets if isinstance(t, ast.Name))
    for node in writer_nodes:   # ANY changed line of a writer is held wherever the writer is called (a runtime function can run it before `command_registrars` collects)
        if _spans(node, added):
            found.append(("a changed line of a function that writes a namespace", "calls", "a statement that may write the module's namespace"))
    for node in ast.walk(head):   # a star import binds names no pattern can see
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names) and _spans(node, added):
            found.append(("a star import", "imports", "a statement that may write the module's namespace"))
    for node in _import_time_statements(head.body):   # MODULE-LEVEL DEFAULT-DENY: `command_registrars` collects registrars out of the module's globals by name, so any added module-level statement that may write the namespace is held:
        if not _spans(node, added):                # a bare expression statement (`globals().update(...)`, `setattr(sys.modules[__name__], ...)`, `exec(...)`: a call is not a binding the other rules can read), and any statement that
            continue                                # reaches the namespace by a call or an attribute (`globals`, `vars`, `locals`, `setattr`, `exec`, `eval`, `compile`, `__import__`, `__dict__`, `modules`)
        if isinstance(node, ast.Expr) and not isinstance(node.value, ast.Constant):
            found.append(("a module-level expression statement", "calls", "a statement that may write the module's namespace"))
        for x in _shallow_nodes(node):
            reach = reaches(x) and not isinstance(x, (ast.Global, ast.Nonlocal))
            reach = reach or (isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in writers) or (isinstance(x, ast.Attribute) and x.attr in writers)
            if reach:
                found.append(("a module-level namespace reach", "calls", "a statement that may write the module's namespace"))
                break
    for node in ast.walk(head):   # a write into the namespace at module level that names a registrar (or a key that cannot be read)
        if isinstance(node, ast.Subscript) and _spans(node, added) and isinstance(node.ctx, (ast.Store, ast.Del)) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Name) and node.value.func.id in ("globals", "vars"):
            key = node.slice
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)) or any(p.fullmatch(key.value) for p in patterns):
                found.append(("a module-level write into globals()", "rebinds", "a registrar bound in a form that cannot be read"))
    table = set((allow or {}).get("pure_builtins", [])) | named
    for tree, numbers in ((head, set(lines[1])), (base, set(lines[0]))):   # a changed line of anything that binds a name of the allow table (a helper's def and its decorators, a rebinding, an import, one under an `if`): it is not read as part of a registrar, so it holds
        for node in _module_statements(tree.body):
            for bound in _bound_here(node):
                if bound in table and _spans_from_decorators(node, numbers):
                    found.append(("a name of the allow table", "rebinds", bound))
    result = Findings(sorted(set(found)))
    result.closure = frozenset(names)
    return result, None


def shadow_names(rule):
    """The names of the clear's closure the matrix carries: the daemon's roots (`closure_roots` and `rating_roots`) and every `module_roots` list (`*` is not a name). The matrix is the only
    source: a binding the closure reads that is not one of these names is NOT known here (a stated limit, #1339; CI's `tools/derive-closure --check` lists the part that holds it)."""
    names = {r for r in daemon_roots(rule) if isinstance(r, str) and r != "*"}
    for roots in (rule.get("module_roots") or {}).values():
        names |= {r for r in roots if isinstance(r, str) and r != "*"}
    return names


_MATCH_KINDS = tuple(getattr(ast, n) for n in ("MatchAs", "MatchStar", "MatchMapping") if hasattr(ast, n))
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _store_names(node):
    """The names an expression, target, argument list or pattern binds when it runs: every Name stored or deleted, every walrus target and every pattern capture, found by walking ALL its children
    (not a list of fields). A lambda's BODY and a comprehension's loop variables are their own scope (not entered; a walrus inside a comprehension binds in the enclosing scope), but a lambda's
    defaults and annotations run now and are walked."""
    found, todo = set(), [node]
    while todo:
        x = todo.pop()
        if isinstance(x, ast.Lambda):
            todo.append(x.args)
            continue
        if isinstance(x, _COMPREHENSIONS):
            found.update(n.target.id for n in ast.walk(x) if isinstance(n, ast.NamedExpr))
            continue
        if isinstance(x, ast.Name) and isinstance(x.ctx, (ast.Store, ast.Del)):
            found.add(x.id)
        elif isinstance(x, ast.NamedExpr):
            found.add(x.target.id)
        elif _MATCH_KINDS and isinstance(x, _MATCH_KINDS):
            for attr in ("name", "rest"):
                if isinstance(getattr(x, attr, None), str):
                    found.add(getattr(x, attr))
        todo.extend(ast.iter_child_nodes(x))
    return found


def bound_names(tree):
    """THE CLOSED RULE of the shadow check (#1339): the names a file BINDS at module level are every name bound by ANY statement that runs when the file is imported, found by walking the module
    body through its blocks (if/try/for/while/with/match, every field of them: tests, targets, handler types, patterns, guards, bodies) and NOT into function or class bodies. Everything of a def or
    class that runs at definition counts (its name, decorators, argument defaults and annotations, return annotation, bases and keywords). Bound are: def and class names; Assign, AnnAssign and
    AugAssign targets (nested, starred); Import and ImportFrom aliases (the alias, or the first dotted segment); for, with and except targets; walrus targets anywhere that runs; match captures; names
    deleted by `del`; and every name a `global` declaration names anywhere in the file (how a function binds a module name). The walk takes every child of a statement, so a position nobody listed is
    still read. LIMITS, stated: `exec`/`eval`, writes through `globals()`/`vars()`/`setattr` on the module, bindings made by a call (`globals().update(...)`) and `from x import *` are not followed."""
    found = set()

    def visit(node):
        if isinstance(node, DEF_KINDS):
            found.add(node.name)
            for part in list(node.decorator_list) + ([node.args, node.returns] if not isinstance(node, ast.ClassDef) else list(node.bases) + list(node.keywords)):
                if part is not None:
                    found.update(_store_names(part))
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.update((a.asname or a.name).split(".")[0] for a in node.names if a.name != "*")
            return
        if isinstance(node, ast.ExceptHandler) and node.name:
            found.add(node.name)
        for _field, value in ast.iter_fields(node):
            for item in (value if isinstance(value, list) else [value]):
                if isinstance(item, (ast.stmt, ast.ExceptHandler)) or (hasattr(ast, "match_case") and isinstance(item, ast.match_case)):
                    visit(item)
                elif isinstance(item, ast.AST):
                    found.update(_store_names(item))   # a test, target, iterable, context manager, handler type, pattern or guard

    for statement in tree.body:
        visit(statement)
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            found.update(node.names)
    return found


def shadow_refusals(base_src, head_src, names, filename):
    """[reasons] a change ADDS, in `filename`, a bound name (`bound_names`: the head's set minus the base's) that is in `names`, the clear's closure: the assembled daemon is one namespace, so a second
    binding of a closure name in another file (or a nested one in the same file) could take the place of the one the clear judged. A file that does not parse before or after the change: refused."""
    try:
        added = bound_names(ast.parse(head_src)) - bound_names(ast.parse(base_src))
    except (SyntaxError, ValueError):
        return ["%s does not parse before or after the change: refused" % filename[:80]]
    return sorted("it adds %s in %s, a name of the clear's closure (a second definition of it could take its place): refused" % (name, _clean(filename[:80]))   # cleaned here too: the reason is also returned by delegated_clear_map
                  for name in set(names) & added)


def decode_blob(answer):
    """The text of a `git/blobs` answer, or None when it is not a complete base64 text."""
    if not isinstance(answer, dict) or answer.get("encoding") != "base64" or not isinstance(answer.get("content"), str):
        return None
    try:
        return base64.b64decode("".join(answer["content"].split()), validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return None
