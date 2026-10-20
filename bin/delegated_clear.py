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


def judge(block, cells, entries, report, threshold):
    """{"level": the score or None, "reasons": [why refused]} (no reasons: the score is at or below `threshold`, so the clear may go on). `block` is the matrix's score block, `cells` its `cells` (severity ->
    probability -> {"level"}), `entries` the ledger's entries (the caller filtered nothing: this checks kind, reviewer, pr, EXACT head and the recorded role again), `report` the owner-hold report (`pr`, `head`,
    `top_tier_hits` {class: [paths]}). The score is the HIGHEST level of the counted records (a later, lower one never lowers it), taken from the matrix cell of the recorded severity and probability, never from
    a word of the reviewer's. A floor class hit makes `floor.lowest_level` the lowest score allowed, and the rating's reason must name that class's rule id. Anything it cannot read is refused, never Low. #835."""
    if not isinstance(block, dict) or not isinstance(cells, dict) or not isinstance(report, dict):
        return {"level": None, "reasons": ["the score rule or the rating is unknown: refused"]}
    ladder = block["ladder"]
    if threshold not in block["delegable_levels"] or threshold not in ladder:
        return {"level": None, "reasons": ["the threshold %r is unknown: refused" % (threshold,)]}
    counted, bad = [], 0
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("kind") != "review_record" or entry.get("reviewer") != "impact" or entry.get("pr") != report.get("pr") or entry.get("head") != report.get("head") or not report.get("head"):
            continue
        if entry.get("by_role") not in block["rating_roles"]:
            continue   # the pull request's author, the Chief of Staff, any other role (or a record made before roles were kept) is not the independent rating
        level = ((cells.get(entry.get("impact_severity")) or {}).get(entry.get("impact_probability")) or {}).get("level") if isinstance(entry.get("impact_severity"), str) and isinstance(entry.get("impact_probability"), str) else None
        if level not in ladder:
            bad += 1
        else:
            counted.append((ladder.index(level), level, entry))
    if bad:
        return {"level": None, "reasons": ["a rating at this head has no severity and probability the matrix knows: the score is unknown, refused"]}
    if not counted:
        return {"level": None, "reasons": ["there is no independent impact rating at this head: the score is unknown, refused"]}
    top = max(i for i, _, _ in counted)
    level = ladder[top]
    reasons = []
    hits = report.get("top_tier_hits") or {}
    for rule_id, cls in sorted(block["floor"]["rules"].items()):
        if not hits.get(cls):
            continue
        if top < ladder.index(block["floor"]["lowest_level"]):
            reasons.append("it changes a %s path and the rating %s is below the floor %s: refused" % (cls, level, block["floor"]["lowest_level"]))
        elif not any(i == top and re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(rule_id), str(e.get("impact_reason") or "")) for i, _, e in counted):
            reasons.append("it changes a %s path and the rating's reason does not name the rule %s: the rule is not named, the rating does not count" % (cls, rule_id))
    if level not in block["delegable_levels"]:
        reasons.append("the score is %s: %s is the owner's" % (level, level))
    elif ladder.index(level) > ladder.index(threshold):
        reasons.append("the score %s is above the threshold %s" % (level, threshold))
    return {"level": level, "reasons": reasons}


def daemon_roots(rule):
    """The daemon file's roots of the clear closure: `closure_roots` (what the clear command runs) and `rating_roots` (the code that WRITES and PARSES the independent rating the score is read from, #835): a change to a definition reachable from any of them is the owner's own clear."""
    return sorted(set(rule["closure_roots"]) | set(rule.get("rating_roots") or []))


# ---- the changed-file list of a pull request: read in full, or refused with ONE named reason (#967)

FILE_PAGE = 100       # GitHub's pull request files API answers at most this many a page
FILE_LIMIT = 3000     # ...and lists at most this many files for one pull request, however many pages are asked for
FILE_PAGES = FILE_LIMIT // FILE_PAGE + 1   # the last page asked (the one after the 3000th file) is how the API says it is done

# THE one table of why a file list was not read in full: the kind, and the words a person reads (and the audit holds). A kind is never reworded by a caller; a test derives the kinds
# used in the code from this table and fails when one is missing, and the documentation carries one line for each.
FILE_REFUSALS = {
    "read_failed": "the changed files could not be read after one retry (%s)",
    "not_a_list": "GitHub's answer for the changed files was not a list (%s)",
    "over_limit": "the pull request has more than %d changed files, more than GitHub lists: too many to read in full",
    "head_moved": "the pull request's head moved while it was being checked (%s, then %s): nothing was cleared",
    "cut": "GitHub itself says the file list is cut: it listed %d of %d changed files",
    "bad_entry": "page %d, position %d of the changed files is not a file entry (%s)",
    "no_count": "the pull request's count of changed files was missing, so the list cannot be checked against it",
    "no_source": "%d renamed or copied file(s) with no known source path (%s)",
    "not_recorded": "the file list was not read in full (no reason was recorded)",
}


class FileListError(Exception):
    """The file list was not read in full. `kind` is a key of FILE_REFUSALS; `str(err)` is its words."""

    def __init__(self, kind, *args):
        self.kind = kind
        super().__init__(FILE_REFUSALS[kind] % args)

    def cause(self):
        """the cause record of this refusal: the kind `file_list`, the rule being the file-list kind"""
        return {"kind": "file_list", "path": None, "rule": self.kind, "text": str(self)}


def error_class(err):
    """One short phrase for a failed read: the class (a timeout or gh that could not run, a gh failure with its HTTP status, an answer that is not JSON, an answer over the size cap) and a clipped detail."""
    detail = " ".join(str(err).split())[:100]
    status = re.search(r"\bHTTP (\d{3})\b", detail)
    reason = getattr(err, "reason", None)
    if isinstance(err, json.JSONDecodeError):
        return "the answer was not JSON"
    if reason == "unavailable":
        return "gh unavailable: %s" % detail
    if reason == "gh":
        return "gh failed%s: %s" % (" with HTTP %s" % status.group(1) if status else "", detail)
    if isinstance(err, ValueError):
        return "the answer was rejected: %s" % detail
    return "%s: %s" % (type(err).__name__, detail)


def read_with_retry(get, path, is_good, what=""):
    """`get(path)`, tried twice at most: a failure or an answer that is not what was asked for is tried once more, and the SECOND outcome is the one named."""
    last = None
    for _ in (1, 2):
        try:
            answer = get(path)
        except Exception as err:
            last = FileListError("read_failed", what + error_class(err))
            continue
        if is_good(answer):
            return answer
        last = FileListError("not_a_list", "%sa %s" % (what, type(answer).__name__))
    raise last


def read_pr_files(get, repo, number):
    """(files, pull) of a pull request: EVERY changed file, in order, or FileListError naming why not. `get(path)` is a GET that raises on any failure. The pages are asked until the API says it is done
    (a page of fewer than 100; the page after the 3000th file is the last a full list can need), each page once more on a failure, and a list is never returned in part. The pull request's own count
    of changed files is read first: more than 3000 is refused before any page is read, and a list whose length differs from it at the end is the ONE case GitHub itself says is cut."""
    pull = read_with_retry(get, "repos/%s/pulls/%d" % (repo, number), lambda a: isinstance(a, dict), "the pull request: ")
    total = pull.get("changed_files")
    total = total if isinstance(total, int) and not isinstance(total, bool) else None
    if total is not None and total > FILE_LIMIT:
        raise FileListError("over_limit", FILE_LIMIT)
    files = []
    for page in range(1, FILE_PAGES + 1):
        got = read_with_retry(get, "repos/%s/pulls/%d/files?per_page=%d&page=%d" % (repo, number, FILE_PAGE, page), lambda a: isinstance(a, list), "page %d: " % page)
        for position, entry in enumerate(got, 1):   # a list is not yet a list of files: an entry that is not one is refused with its place, never dropped (dropping it would make the count differ and blame GitHub)
            if not isinstance(entry, dict):
                raise FileListError("bad_entry", page, position, "a %s, not an object" % type(entry).__name__)
            if not isinstance(entry.get("filename"), str) or not entry["filename"]:
                raise FileListError("bad_entry", page, position, "no file name")
            files.append(entry)
        if len(got) < FILE_PAGE:
            break
    else:
        raise FileListError("over_limit", FILE_LIMIT)   # the page after the 3000th file still has files
    if total is None:
        raise FileListError("no_count")
    if total != len(files):
        raise FileListError("cut", len(files), total)
    return files, pull


def list_words(report):
    """The words for a report (`pr_floor_report`) whose file list is not whole: the reason recorded for it, else what its own fields say; the fallback wording never says "cut"."""
    if report.get("list_problem"):
        return report["list_problem"]
    if report.get("unknown"):
        return report["unknown"]
    if report.get("renamed_no_source"):
        return FILE_REFUSALS["no_source"] % (len(report["renamed_no_source"]), ", ".join(str(x) for x in report["renamed_no_source"]))
    if report.get("complete") is False and all(isinstance(report.get(k), int) for k in ("files_read", "files_total")):
        return "GitHub listed %s of %s changed files" % (report.get("files_read"), report.get("files_total"))
    return FILE_REFUSALS["not_recorded"]


# THE one table of the causes a delegated clear is refused for: the kind, and the rule it comes from (a key of the matrix's `owner_hold.delegated_clear` block, or the name of the check that made it).
# A refusal is a list of cause records {"kind", "path", "rule", "text"} made by `causes_of`; the readable reason is the `text` of each, joined: ONE table, so the audit's records and the person's words
# are the same rows. A test derives every kind used in the code and fails when one is missing here or has no driver.
CAUSE_RULES = {
    "rule_unreadable": "delegated_clear",
    "rule_malformed": "delegated_clear",
    "decision_unreadable": "owner_hold",
    "file_list": "file_list",
    "never_class": "never_path_classes",
    "no_review": "review",
    "review_no_class": "review",
    "review_top_class": "top_tier_review_classes",
    "score": "score",
    "files_unreadable": "file_list",
    "file_unreadable": "file_list",
    "path_unreadable": "file_list",
    "clear_code": "clear_code_paths",
    "new_file": "new_file_globs",
    "closure_unmapped": "closure_files",
    "closure": "closure_files",
    "order": "delegated_clear",
    "owner_hold_label": "hold_labels",
    "person_hold_label": "hold_labels",
}


AUDIT_LINE_MAX = 1 << 20   # characters of ONE refusal the audit and the terminal keep: the audit file rotates at 20 MiB, so one line is held to a twentieth of it. The only bound; when it is reached the line says so.


def whole_line(text, limit=AUDIT_LINE_MAX):
    """`text` on one line, with control characters (an escape sequence in a file name) turned to spaces and nothing cut until the one physical bound, which says how much was left out."""
    line = " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(text or "")).split())
    if len(line) <= limit:
        return line
    return line[:limit] + " ... (%d more characters not kept: one audit line holds at most %d)" % (len(line) - limit, limit)


def audit_parts(why, causes):
    """{"why", "causes"} for the audit detail: the whole reason, and the cause records (control characters out of their text and path) up to the same bound; `causes_omitted` says how many did not fit."""
    kept, size = [], 0
    for item in causes:
        record = dict(item, text=whole_line(item.get("text")), path=whole_line(item["path"]) if isinstance(item.get("path"), str) else item.get("path"))
        size += len(json.dumps(record))
        if size > AUDIT_LINE_MAX:
            break
        kept.append(record)
    parts = {"why": whole_line(why), "causes": kept}
    if len(kept) < len(causes):
        parts["causes_omitted"] = len(causes) - len(kept)
    return parts


def cause(kind, text, path=None):
    """One cause record: the kind (a key of CAUSE_RULES), the path it is about (or None), the rule it comes from and the words."""
    return {"kind": kind, "path": path, "rule": CAUSE_RULES[kind], "text": text}


def causes_of(rule, report, files, matches=None, closure=None, scored=None):
    """[cause records] a delegated clear is refused for this pull request; [] when none. `rule` is the matrix's `owner_hold.delegated_clear` block; `report` is the owner-hold report the
    daemon uses (`pr_floor_report`: its decision, its top tier path hits by class, the independent review's class, whether the file list is complete); `files` is the pull request's
    changed files ([{"filename", "previous_filename", "status"}]) or None when they could not be read. `matches(path, globs)` is the owner-hold floor's OWN path matcher (case-,
    normalisation- and spelling-insensitive: `./bin/x`, `bin//x`, `BIN/X`, a `..`, a rename's source all read as the file they name): the caller passes it, this module holds no second one.
    `closure` is {file name: [reasons] or None}: what `closure_refusals` said for each changed file of `closure_files` (None: it could not be mapped). `scored` is `judge`'s answer for an order with a
    max_level: then the score is the only rule about the pull request's classes (the path-class and review-class refusals below are not made), and the machinery refusals stay."""
    if not isinstance(rule, dict) or not isinstance(report, dict) or matches is None:
        return [cause("rule_unreadable", "the delegated-clear rule or the owner-hold decision cannot be read")]
    for key in ("never_path_classes", "top_tier_review_classes", "clear_code_paths", "new_file_globs", "closure_files"):
        if not isinstance(rule.get(key), list) or not all(isinstance(x, str) for x in rule[key]):
            return [cause("rule_malformed", "the delegated-clear rule is malformed (%s)" % key)]
    if report.get("owner_decision") not in ("hold", "second-review", "none"):
        return [cause("decision_unreadable", "the owner-hold decision cannot be read")]
    if report.get("complete") is not True or report.get("unknown") or report.get("renamed_no_source"):
        return [cause("file_list", "the pull request's changes cannot be read in full (%s)" % list_words(report))]
    found = [cause("score", text) for text in (scored["reasons"] if isinstance(scored, dict) else [])]
    hits = report.get("top_tier_hits") or {}
    for cls in sorted(hits) if scored is None else ():
        if cls.lower() in {c.lower() for c in rule["never_path_classes"]}:
            found.append(cause("never_class", "it changes a %s path (%s): that stays the owner's own clear" % (cls, ", ".join(hits[cls])), ", ".join(hits[cls])))
    if report.get("review_action") is None and scored is None:
        found.append(cause("no_review", "no independent review is recorded at this head: unknown is refused"))
    review = report.get("review_class")
    if review is None and report.get("review_action") == "hold" and scored is None:
        found.append(cause("review_no_class", "the independent review holds it with no class named: unknown is refused"))
    if isinstance(review, str) and review.lower() in {c.lower() for c in rule["top_tier_review_classes"]} and scored is None:
        found.append(cause("review_top_class", "the independent review's class is %s, a top tier class: that stays the owner's own clear" % review))
    if files is None:
        found.append(cause("files_unreadable", "the changed files cannot be read, so a change to the clear code cannot be ruled out"))
        return found
    named = set(report.get("changed_paths") or [])
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            found.append(cause("file_unreadable", "a changed file cannot be read: refused"))
            continue
        both = [x for x in (item.get("filename"), item.get("previous_filename")) if x is not None]
        if any(not isinstance(x, str) or "\x00" in x for x in both):
            found.append(cause("path_unreadable", "a changed path cannot be read (%r): refused" % str(both)))
            continue
        for path in both:
            if matches(path, rule["clear_code_paths"]):
                found.append(cause("clear_code", "it changes a file that is the clear or hold machinery or that the daemon loads (%s): that stays the owner's own clear" % path, path))
        if any(matches(path, rule["new_file_globs"]) for path in both) and (item.get("status") in ("added", "removed", "renamed", "copied") or item.get("previous_filename")):
            found.append(cause("new_file", "it adds, deletes or renames a file under a directory the daemon loads at start (%s): that stays the owner's own clear" % item["filename"], item["filename"]))
        for path in both:
            if matches(path, rule["closure_files"]):
                mapped = (closure or {}).get(item["filename"])
                if mapped is None:
                    found.append(cause("closure_unmapped", "the change to %s cannot be mapped to its definitions: refused" % path, path))
                else:
                    found.extend(cause("closure", text, path) for text in mapped)
    for path in sorted(p for p in named if isinstance(p, str)):
        if matches(path, rule["clear_code_paths"]):
            found.append(cause("clear_code", "it changes a file that is the clear or hold machinery or that the daemon loads (%s): that stays the owner's own clear" % path, path))
    seen, out = set(), []
    for item in sorted(found, key=lambda c: c["text"]):   # the readable list is sorted and has no repeat, as it always was; the records are the same rows
        key = (item["kind"], item["path"], item["text"])
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def refusals(rule, report, files, matches=None, closure=None, scored=None):
    """[reasons] a delegated clear is refused for this pull request; [] when none: the words of `causes_of`, sorted, one for each distinct text."""
    return sorted(set(c["text"] for c in causes_of(rule, report, files, matches, closure, scored)))


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


def decode_blob(answer):
    """The text of a `git/blobs` answer, or None when it is not a complete base64 text."""
    if not isinstance(answer, dict) or answer.get("encoding") != "base64" or not isinstance(answer.get("content"), str):
        return None
    try:
        return base64.b64decode("".join(answer["content"].split()), validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return None
