"""Clearing a held PR by saying so in the owner's chat with the Chief of Staff (ask #457), slice 1: the offers and the deterministic checks.

This slice decides nothing and changes nothing on GitHub. It holds the three things the later slices stand on:

* the OFFERS: every open PR held for the owner, each at the head the owner was last shown. `refresh_offers` IS the showing: the later hook calls it with the
  heads of the same read that builds the owner's Decisions list, so a head recorded there is a head he can see; after a lost file nothing recorded is
  `shown` until `show()` has put it in front of him. A hold is "owner-tier" when the PR carries the
  impact, credential or policy hold label or the captain marker label, however it was applied. A PR that also carries any OTHER hold label
  (security, contract, prod-config, other-area) is never offered: clearing it would lift only part of what holds it;
* the PERSON'S messages: a user message in the Chief of Staff's session WITHOUT fleet metadata, never one an agent wrote, new since the last
  read and recent;
* the CHECKS that no model can waive: the PR number must be written in the message and must be one of the offers. The later slices add the
  judge (it can only confirm or veto, never widen) and the clear itself.

A message that names no number, a number that is no offer, or several offers is a QUESTION for the owner, never a clear. The module reads and
writes only its own private file in the state directory and holds no credential. Python 3.9 standard library only.
"""
import fcntl
import json
import os
import re
import tempfile
import time
from pathlib import Path

OFFERS_FILE = "chat-clear-offers.json"
OFFERS_MAX_BYTES = 262144
OFFERS_MAX = 500
HOLD_KEYS = ("hold-impact", "hold-credential", "hold-policy")     # the owner-tier hold labels (config-independent keys; the names get the user prefix)
CAPTAIN_KEY = "merge-captain"
OTHER_HOLD_KEYS = ("hold-security", "hold-contract", "hold-prod-config", "hold-other-area")
MESSAGE_MAX_AGE = 900          # seconds: an older message is not acted on
MESSAGE_SKEW = 60              # seconds: a message dated later than now + this is not read (its time is not to be trusted)
MESSAGES_SCANNED = 50          # prompts looked at per read
TEXT_MAX = 2000                # characters of a message kept and scanned
SUMMARY_MAX = 200
LOCK_FILE = "chat-clear-offers.lock"
LOCK_SECONDS = 5
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
DIGITS = re.compile(r"[0-9]+")   # ASCII digits only
OFFER_TTL = 7 * 86400          # seconds: an offer older than this is not clearable (the owner is shown it again first)


def _label_names(labels):
    out = []
    for label in labels or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            out.append(name)
    return out


def owner_tier_labels(names, labels):
    """The owner-tier labels `labels` carries: the impact, credential and policy hold labels and the captain marker label."""
    wanted = {names[key] for key in HOLD_KEYS + (CAPTAIN_KEY,) if key in names}
    return [name for name in _label_names(labels) if name in wanted]


def is_offerable(names, labels):
    """True when the PR is held for the owner and ONLY by labels a chat clear may lift: it carries at least one owner-tier label and no other hold."""
    if any(key not in names for key in HOLD_KEYS + (CAPTAIN_KEY,) + OTHER_HOLD_KEYS):
        return False   # a label this module cannot name is a hold it cannot see: nothing is offered
    present = set(_label_names(labels))
    if any(names[key] in present for key in OTHER_HOLD_KEYS):
        return False
    return bool(owner_tier_labels(names, labels))


class OffersBusy(Exception):
    """The offers are locked by another process and the wait ran out: nothing was changed."""


class _locked:
    """One flock over a read-modify-write of the offers file: `refresh_offers` and `show` cannot interleave."""

    def __init__(self, state_dir, seconds=LOCK_SECONDS):
        self.path, self.seconds, self.fd = Path(state_dir) / LOCK_FILE, seconds, None

    def __enter__(self):
        self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.seconds
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    raise OffersBusy("the chat-clear offers are locked by another process (waited %s s)" % self.seconds) from None
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        return False


def _valid_offer(offer):
    """True only for a whole, well-typed offer: a head, `shown` exactly a boolean, `shown_at` a whole number, `labels` a list of strings and `summary` a
    string. A damaged entry is never repaired or defaulted (above all not to `shown`): it is dropped."""
    return (isinstance(offer, dict) and isinstance(offer.get("head"), str) and bool(offer["head"]) and isinstance(offer.get("shown"), bool)
            and isinstance(offer.get("shown_at"), int) and not isinstance(offer["shown_at"], bool) and offer["shown_at"] >= 0
            and isinstance(offer.get("labels"), list) and all(isinstance(l, str) for l in offer["labels"]) and isinstance(offer.get("summary"), str))


def _load(state_dir):
    """(offers, problem, damaged). `problem` is None for a good file or no file, else a short reason: an unreadable, oversized or malformed file offers
    nothing. `damaged` is the set of keys whose entry was dropped one by one (a bad entry neither invalidates nor validates the others)."""
    path = Path(state_dir) / OFFERS_FILE
    try:
        if path.stat().st_size > OFFERS_MAX_BYTES:
            return {}, "the offers file is larger than %d bytes" % OFFERS_MAX_BYTES, set()
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None, set()
    except (OSError, ValueError) as err:
        return {}, "the offers file cannot be read (%s)" % type(err).__name__, set()
    offers = data.get("offers") if isinstance(data, dict) else None
    if not isinstance(offers, dict):
        return {}, "the offers file is not an object of offers", set()
    good, damaged = {}, set()
    for key, offer in offers.items():
        if not (isinstance(key, str) and re.fullmatch(r"[^/#\s]+/[^/#\s]+#[0-9]{1,9}", key)):
            damaged.add(str(key)[:200])   # a key that is no PR: said, never silently lost
            continue
        if _valid_offer(offer) and len(good) < OFFERS_MAX:
            good[key] = offer
        else:
            damaged.add(key)   # a damaged entry, or a whole one past the cap: neither is trusted again as shown
    return good, None, damaged


def read_offers(state_dir):
    """{"owner/repo#N": {"head", "shown", "shown_at", "labels", "summary"}}, {} when there is no file or it cannot be used: nothing is offered, so
    nothing can be cleared (it fails toward hold)."""
    return _load(state_dir)[0]


def write_offers(state_dir, offers):
    """Atomic, private (0600) and durable: a temporary file in the same directory, flushed and synced, renamed over the old one."""
    directory = Path(state_dir)
    fd, tmp = tempfile.mkstemp(prefix=OFFERS_FILE + ".", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"offers": offers}, handle, sort_keys=True, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(directory / OFFERS_FILE))
        dir_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dir_fd)   # the rename itself survives a crash
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


SECRET_SHAPES = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|sk-[A-Za-z0-9_-]{16,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{12,}"
                           r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}|\b[A-Fa-f0-9]{40,}\b|\b[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=]))")


def _clean(text, limit):
    """One line, control characters out, anything shaped like a token or key replaced by `[secret]`, at most `limit` characters (and always writable text)."""
    text = SECRET_SHAPES.sub("[secret]", " ".join(CONTROL.sub(" ", str(text or "")).split()))
    return text[:limit].encode("utf-8", "replace").decode("utf-8")   # a lone surrogate cannot be written: it becomes ?


def _quarantine(state_dir, now):
    """Set a file that cannot be used aside (`.corrupt-<time>`), never overwrite it: what it held can be recovered by hand."""
    path = Path(state_dir) / OFFERS_FILE
    for n in range(100):
        aside = path.with_name("%s.corrupt-%d%s" % (OFFERS_FILE, int(now), "" if n == 0 else "-%d" % n))
        if not aside.exists():
            os.rename(str(path), str(aside))
            return aside.name
    raise OSError("too many quarantined offers files")


def refresh_offers(state_dir, holds, names, now, lock_seconds=LOCK_SECONDS):
    """Bring the offers to what is held now. `holds` is a list of {"pr": "owner/repo#N", "head", "labels", "summary"} read from the open PRs.

    * a held PR that is offerable and has no offer is recorded at its CURRENT head, with `shown_at` = now;
    * a PR that already has an offer KEEPS the head the owner was shown: a push after it is a mismatch the later slices put to the owner, and
      `show()` moves the shown head only when the new one has been put in front of him;
    * a PR that is no longer held (or no longer offerable) loses its offer;
    * a file that cannot be used is set aside (`.corrupt-<time>`), and what is recorded afterwards is `shown: false`: the owner's memory of what he
      was shown is lost, so nothing recorded from it can be cleared until `show()` has put it in front of him;
    * at most OFFERS_MAX offers: the ones already there come first and `capped` says some held PR was left out.
    Returns {"offers", "added", "dropped", "problem", "quarantined", "capped", "damaged"} (`damaged`: entries dropped one by one; a PR whose entry was damaged is
    recorded again as `shown: false`); the file is written only when something changed.
    Raises OffersBusy when the lock wait runs out."""
    with _locked(state_dir, lock_seconds):
        before, problem, damaged = _load(state_dir)
        quarantined = _quarantine(state_dir, now) if problem else None
        after, added, capped = {}, [], False
        usable = []
        for hold in holds or []:
            pr, head = hold.get("pr"), hold.get("head")
            if isinstance(pr, str) and isinstance(head, str) and head and is_offerable(names, hold.get("labels")):
                usable.append(hold)
        usable.sort(key=lambda hold: hold["pr"] not in before)   # an offer that exists is kept before a new one is taken
        for hold in usable:
            pr = hold["pr"]
            if pr in after:
                continue   # the same PR twice in one read: the first is kept
            if len(after) >= OFFERS_MAX:
                capped = True
                break
            labels = owner_tier_labels(names, hold.get("labels"))
            summary = _clean(hold.get("summary"), SUMMARY_MAX)
            kept = before.get(pr)
            if kept is None:
                lost = bool(problem) or pr in damaged   # a lost file or a damaged entry: what he was shown is not known
                after[pr] = {"head": hold["head"], "shown": not lost, "shown_at": int(now), "labels": labels, "summary": summary}
                added.append(pr)
            else:
                after[pr] = dict(kept, labels=labels, summary=summary or kept.get("summary", ""))
        dropped = sorted(set(before) - set(after))
        if after != before or damaged:   # damaged entries are written out of the file, so the loss is said once and not re-read forever
            write_offers(state_dir, after)
    return {"offers": after, "added": added, "dropped": dropped, "problem": problem, "quarantined": quarantined, "capped": capped,
            "damaged": sorted(damaged)}


def show(state_dir, pr, head, now, lock_seconds=LOCK_SECONDS):
    """The owner has just been shown `pr` at `head` (a question that names it): the offer's shown head moves to it. False when `pr` has no offer, or when
    the file holds a damaged entry (a refresh must rewrite it first, so the memory of what was damaged is not lost by this write).
    Raises OffersBusy when the lock wait runs out."""
    with _locked(state_dir, lock_seconds):
        offers, _, damaged = _load(state_dir)
        if damaged:   # the file has entries nobody can trust: only a refresh (the showing) rewrites it, and it records them as not shown
            return False
        if pr not in offers or not isinstance(head, str) or not head:
            return False
        offers[pr] = dict(offers[pr], head=head, shown=True, shown_at=int(now))
        write_offers(state_dir, offers)
        return True


def _is_agents(message):
    """True unless the message is provably the person's. It fails CLOSED: a `fleet` key in `metadata` or in `payload.metadata`, of any type, makes it an
    agent's, and so does a `metadata` that is neither absent nor an object."""
    places = [message.get("metadata")]
    payload = message.get("payload")
    if payload is not None:
        if not isinstance(payload, dict):
            return True
        places.append(payload.get("metadata"))
    for meta in places:
        if meta is None:
            continue
        if not isinstance(meta, dict) or "fleet" in meta:
            return True
    return False


def persons_text(message):
    """The text of a message that is provably a person's PROMPT, else None. Fails CLOSED. All of these must hold: it is not an agent's (`_is_agents`); its `role` and
    its `type`, whichever it has, are exactly `user` (not assistant, system, synthetic, a tool result or a compaction summary) and it has at least one; its
    text is a `text` string, a pending inbox item's `payload.text`, or `content` parts that are ALL of type `text` (any other part, a tool call or result, a
    file, a summary, makes it not a prompt); and `text` and `content`, when both are given, say the same."""
    if not isinstance(message, dict) or _is_agents(message):
        return None
    kinds = [message[k] for k in ("role", "type") if k in message]
    if not kinds or any(kind != "user" for kind in kinds):
        return None
    given = message.get("text")
    parts = message.get("content")
    joined = None
    if "content" in message:
        if not isinstance(parts, list) or not parts or not all(isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str) for p in parts):
            return None
        joined = "\n".join(p["text"] for p in parts)
    if isinstance(given, str):
        if joined is not None and joined != given:
            return None
        return given
    if given is not None:
        return None
    if joined is not None:
        return joined
    payload = message.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("text"), str):
        return payload["text"]
    return None


def person_messages(prompts, seen, now, created_of, max_age=MESSAGE_MAX_AGE):
    """The person's own NEW messages out of `prompts` (a Chief of Staff session's user messages, as the engine read them), oldest first, as
    [{"id", "text", "created"}]. Only what `persons_text` accepts is returned: a message with fleet metadata, any other role or type, or a part that is not plain
    text is never the owner's, whatever it says. One without an id, already in `seen`, with no readable time, older than `max_age` seconds or dated more than
    MESSAGE_SKEW seconds ahead is skipped. At most MESSAGES_SCANNED prompts are looked at and TEXT_MAX characters of each kept.
    `created_of(message)` gives the epoch seconds of a message."""
    out = []
    for message in list(prompts or [])[:MESSAGES_SCANNED]:
        text = persons_text(message)
        if text is None:
            continue
        message_id = message.get("id")
        created = created_of(message)
        if isinstance(created, bool) or not isinstance(created, (int, float)) or created != created:
            continue   # no readable time, or not a number: skipped, never an error
        if not isinstance(message_id, str) or message_id in seen or now - created > max_age or created - now > MESSAGE_SKEW:
            continue
        out.append((created, {"id": message_id, "text": text[:TEXT_MAX], "created": created}))
    return [item for _, item in sorted(out, key=lambda row: row[0])]


def numbers_in(text):
    """Every number written in `text`, in order, once each: EVERY run of digits, whatever is around it (`366-367`, `366/367`, `366, 389`, `366..389`, `366+367`,
    `366&367`, `366 and 389` are two numbers; so is `0.97`, which fails toward asking). One message names one pull request."""
    seen = []
    for found in DIGITS.finditer((text or "")[:TEXT_MAX]):   # bounded: no digit run longer than the text cap
        number = int(found.group(0))
        if number not in seen:
            seen.append(number)
    return seen


def _standalone(text, number):
    """True when every run of digits that spells `number` stands alone: not inside a word (`abc366`, `366abc`, `366_1`) and without a leading zero (`0366`)."""
    for found in DIGITS.finditer(text or ""):
        if int(found.group(0)) != number:
            continue
        before = text[found.start() - 1] if found.start() else " "
        after = text[found.end()] if found.end() < len(text) else " "
        if before.isalnum() or before == "_" or after.isalnum() or after == "_" or (len(found.group(0)) > 1 and found.group(0).startswith("0")):
            return False
    return True


def timing_problem(offer, created, now):
    """None when the message may clear this offer, else why not: `no_time` (a time is not known, or lies more than MESSAGE_SKEW seconds ahead of the clock), `before_offer` (it was written before the
    owner was shown the offer, or at the same second: only a message AFTER the showing counts) or `expired` (the offer is older than OFFER_TTL)."""
    if isinstance(created, bool) or not isinstance(created, (int, float)) or created != created or not isinstance(now, (int, float)):
        return "no_time"
    shown_at = (offer or {}).get("shown_at")
    if not isinstance(shown_at, int) or isinstance(shown_at, bool) or shown_at > now + MESSAGE_SKEW or created - now > MESSAGE_SKEW:
        return "no_time"   # a time from the future (the offer's or the message's) is not to be trusted
    if created <= shown_at:
        return "before_offer"
    if now - shown_at > OFFER_TTL:
        return "expired"
    return None


PLAIN_WORDS = frozenset(("a also ahead approve approved are be clear cleared fine go goes going good i is it lgtm looks merge merged ok okay pr please ready release released s "
                         "ship shipped thank thanks the through to was you now for with then").split())
LETTERS = re.compile(r"[^\W\d_]+", re.UNICODE)


ALLOWED_MARKS = frozenset("#.,! ")
QUESTION_OPENERS = frozenset(("is", "was", "are", "be", "s"))   # "is 366 good to go" asks by word order, with no question mark


def _plain_clearing(text):
    """True only when the message is a plain clearing sentence: every WORD is in the short allow-list (the number and the marks `#.,!` aside), nothing else but letters,
    digits and white space appears (no emoji, no `/s`, no apostrophe form such as "isn't" or "isn\u2019t", no dash or colon), and no question opener (`is`, `was`, `are`, `be`) comes
    BEFORE the number ("is 366 good to go" is a question without a question mark; "366 is good to go" is a statement). Anything else is not plain, whatever a judge
    would say: the deterministic floor is an allow-list, not a list of hedges."""
    if any(not (ch.isalnum() or ch.isspace() or ch in ALLOWED_MARKS) for ch in text):
        return False
    words = LETTERS.findall(text)
    if not all(word.lower() in PLAIN_WORDS for word in words):
        return False
    first_digit = DIGITS.search(text)
    before = LETTERS.findall(text[:first_digit.start()]) if first_digit else []
    return not any(word.lower() in QUESTION_OPENERS for word in before)


HEDGE = re.compile(r"\?")   # a question mark; everything else the floor refuses is by the allow-list above


def screen(text, offers, created, now):
    """The checks no model can waive. Returns {"verdict": "candidate", "pr"} when the message names exactly ONE number, that number stands alone, exactly one
    offer has it and the message came AFTER the owner was shown that offer, else {"verdict": "question", "why": <code>, "prs": [...]} (the owner is asked,
    nothing is cleared):

    * `no_offers`: nothing is held for the owner;
    * `no_number`: the message names no number (the number is required, whatever a judge would say);
    * `several_numbers`: it has more than one DISTINCT run of digits, however they are joined (`366-367`, `366/367`, `366, 389`): one message clears one PR;
    * `too_long`: the text is TEXT_MAX characters or more (it may have been cut inside a number);
    * `unclear_number`: its one number sits inside a word or a decimal (`abc366`, `3.66`);
    * `not_offered`: the number is not one of the offers;
    * `ambiguous_number`: more than one offer has that number (the same number in two repositories);
    * `hedged`: the message is not a plain clearing sentence: it asks, negates, conditions or says anything outside a short allow-list of clearing words
      ("is 366 good to go?", "366 is not good to go", "366 isn\u2019t good to go", "366 is good to go once CI passes", "366 is good to go except the docs"): a clear is never
      inferred from words like that, whatever a judge would say. A missed clear is one more sentence from the owner;
    * `no_time`, `before_offer`, `expired`: see `timing_problem` (`created` is the message's time, `now` the clock)."""
    text = text if isinstance(text, str) else ""
    prs = sorted(offers or {})
    if not prs:
        return {"verdict": "question", "why": "no_offers", "prs": []}
    if len(text) >= TEXT_MAX:   # `person_messages` cuts at TEXT_MAX, so a message that reaches it may have been cut inside a number: a clearing sentence is never that long
        return {"verdict": "question", "why": "too_long", "prs": prs}
    numbers = numbers_in(text)
    if not numbers:
        return {"verdict": "question", "why": "no_number", "prs": prs}
    if len(numbers) > 1:
        return {"verdict": "question", "why": "several_numbers", "prs": prs}
    if not _standalone(text, numbers[0]):
        return {"verdict": "question", "why": "unclear_number", "prs": prs}
    if HEDGE.search(text) or not _plain_clearing(text):
        return {"verdict": "question", "why": "hedged", "prs": prs}
    matches = [pr for pr in prs if int(pr.rsplit("#", 1)[1]) == numbers[0]]
    if not matches:
        return {"verdict": "question", "why": "not_offered", "prs": prs}
    if len(matches) > 1:
        return {"verdict": "question", "why": "ambiguous_number", "prs": matches}
    problem = timing_problem(offers[matches[0]], created, now)
    if problem:
        return {"verdict": "question", "why": problem, "prs": matches}
    return {"verdict": "candidate", "pr": matches[0]}


def head_matches(offer, current_head):
    """True only when the head the owner was shown is the PR's head now. A push after the offer clears nothing, and neither does an offer
    that was recorded after the offers were lost and has not been shown (`shown: false`)."""
    return isinstance(offer, dict) and offer.get("shown") is True and isinstance(current_head, str) and bool(current_head) \
        and offer.get("head") == current_head


# ----------------------------------------------------------------------------- slice 2: the judge's question and the decision
#
# Jev answers ONE choice question with a probability for every option. It can only CONFIRM what the deterministic checks already found (the message
# names exactly one offered PR, unhedged) or VETO it; it never adds a PR the checks did not name. Anything else (unsure, unreachable, a different
# offer, "several") is a question to the owner, asked once. Nothing here clears anything: slice 3 acts on `decide`'s "clear".

JUDGE_ID = "clears"
OPT_SEVERAL, OPT_ASKS, OPT_NOTHING = "several", "asks", "nothing"
HIGH_CONFIDENCE = 0.9          # the bar for a clear: the config's min_confidence may only raise it
NOTHING_CONFIDENCE = 0.5       # "this message is not about clearing" needs this much before the owner is NOT asked
JUDGE_TEXT_MAX = 1000
JUDGE_OFFERS_MAX = 40
JUDGE_INSTRUCTIONS = (
    "The owner of a software fleet holds some pull requests until he says they may go. He has just sent a message to his assistant. "
    "Does this message clear exactly one of the held pull requests listed below, clear several of them, ask something about them, "
    "or say nothing about clearing a held pull request?")


def judge_request(text, offers):
    """(state, questions) for one judge call: the message and the offers (PR, short head, one-line summary), and the options."""
    shown = [(pr, offers[pr]) for pr in sorted(offers)[:JUDGE_OFFERS_MAX]]
    state = {"message": _clean(text, JUDGE_TEXT_MAX),
             "held": [{"pr": pr, "head": _clean(str(offer.get("head", ""))[:8], 8), "summary": _clean(offer.get("summary"), SUMMARY_MAX)} for pr, offer in shown]}
    criteria = {}
    for pr, offer in shown:
        criteria[pr] = "The message clears %s (%s) and no other held pull request: the owner says it may go." % (pr, _clean(offer.get("summary"), SUMMARY_MAX) or "no summary")
    criteria[OPT_SEVERAL] = "The message clears more than one held pull request, or all of them, or does not say which."
    criteria[OPT_ASKS] = "The message asks a question about a held pull request, or sets a condition, or says it is NOT ready."
    criteria[OPT_NOTHING] = "The message says nothing about clearing a held pull request."
    return state, {JUDGE_ID: {"type": "choice", "instructions": JUDGE_INSTRUCTIONS, "criteria": criteria}}


def judge(jev, text, offers, min_confidence=HIGH_CONFIDENCE):
    """One Jev call. {"choice", "confident", "probabilities", "reason", "jev_choice", ...}; NEVER raises. No judge, an unreachable or erring one, an answer with no
    probabilities and an answer below the bar all give `choice` None and `confident` False: they confirm nothing."""
    bar = min_confidence if isinstance(min_confidence, (int, float)) and not isinstance(min_confidence, bool) and min_confidence == min_confidence \
        and min_confidence <= 1 and min_confidence > HIGH_CONFIDENCE else HIGH_CONFIDENCE   # a bad, NaN or lower bar is the floor: the bar can only be raised
    out = {"choice": None, "confident": False, "probabilities": None, "reason": "", "jev_choice": None, "model": None, "usage": {}, "latency_ms": None}
    if jev is None:
        out["reason"] = "no judge is configured"
        return out
    state, questions = judge_request(text, offers)
    try:
        result = jev.decide(state, questions)
        answer = result["answers"][JUDGE_ID]
    except Exception as err:   # JevError (unreachable, timeout, bad answer, ...): its reason is the only thing kept, never its text
        out["reason"] = "jev %s" % re.sub(r"[^a-z_ ]", "", str(getattr(err, "reason", type(err).__name__)).lower())[:40]
        out["latency_ms"] = getattr(err, "latency_ms", None)
        return out
    if not isinstance(answer, dict):
        out["reason"] = "jev gave an unreadable answer"
        return out
    probabilities = answer.get("probabilities")
    if not (isinstance(probabilities, dict) and all(isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool) for k, v in probabilities.items())):
        out["reason"] = "jev gave no probabilities" if not probabilities else "jev gave an unreadable answer"
        out.update(jev_choice=_clean(answer.get("choice"), 40), model=_clean(result.get("model"), 60), latency_ms=result.get("latency_ms"))
        return out
    out.update(jev_choice=answer.get("choice"), probabilities=probabilities, model=result.get("model"), usage=result.get("usage") or {},
               latency_ms=result.get("latency_ms"))
    if not isinstance(probabilities, dict) or not probabilities:
        out["reason"] = "jev gave no probabilities"
        return out
    choice = answer.get("choice")
    top = probabilities.get(choice, 0.0) if isinstance(choice, str) and choice in questions[JUDGE_ID]["criteria"] else None
    numbers = [v for v in probabilities.values() if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v]
    if top is None or isinstance(top, bool) or not isinstance(top, (int, float)) or top != top or top > 1 or len(numbers) != len(probabilities) or top < max(numbers) \
            or abs(sum(numbers) - 1) > 0.01:
        out["reason"] = "jev gave an unreadable answer"   # the choice is not an option, or is not the most probable one, or a probability is not a number in 0..1
        return out
    if top < bar or sum(1 for v in numbers if v == top) > 1:
        out["reason"] = "unsure: %s at %s is below %.2f or tied" % (_clean(choice, 40), top, bar)
        return out
    out.update(choice=answer["choice"], confident=True, reason="confident: %s at %.2f" % (answer["choice"], top))
    return out


QUESTION_OFFERS_MAX = 10
REASONS = {"no_number": "your message names no pull request number", "several_numbers": "your message names more than one pull request",
           "not_offered": "that number is not one of the held pull requests", "ambiguous_number": "that number is held in more than one repository",
           "hedged": "your message is not a plain clearing sentence (it asks, negates, sets a condition or says something else)",
           "too_long": "your message is too long to read a pull request number from", "unclear_number": "the number in your message sits inside a word",
           "no_time": "the time of your message or of the offer could not be read", "before_offer": "your message was written before you were shown that pull request",
           "expired": "that offer is old and must be shown to you again first", "jev": "I could not be sure you meant to clear it"}


def question_text(why, offers, prs=None, current_heads=None):
    """The ONE question the owner is asked: why nothing was cleared, what is held for him (PR, short head, summary) and how to clear one. Control characters
    are out of every line. `current_heads` {pr: head} adds the PR's new head where it differs from the one he was shown (a push since)."""
    names = sorted(prs if prs else offers)[:QUESTION_OFFERS_MAX]
    lines = ["Nothing was cleared: %s." % REASONS.get(why, why)]
    if names:
        lines.append("Held for you now:")
    for pr in names:
        offer = offers.get(pr) or {}
        line = "- %s at %s: %s" % (pr, _clean(str(offer.get("head", ""))[:8], 8), _clean(offer.get("summary"), SUMMARY_MAX) or "no summary")
        now = (current_heads or {}).get(pr)
        if isinstance(now, str) and now and now != offer.get("head"):
            line += " (it has a new head, %s, since you were shown this one)" % _clean(now[:8], 8)
        lines.append(line)
    if len(offers) > len(names) and not prs:
        lines.append("... and %d more." % (len(offers) - len(names)))
    lines.append("To clear one, say its number, for example: `%s is good to go`." % (names[0].rsplit("#", 1)[1] if names else "366"))
    return "\n".join(lines)


CLEARISH = re.compile(r"\b(?:good to go|ok(?:ay)? to (?:go|merge|ship)|go(?:es)? ahead|clear(?:ed|ing)?|approved?|approving|lgtm|ship(?:ped)?|merge(?:d)?|release[ds]?|unhold|let it (?:go|through)|fine to (?:go|merge)|ready to (?:go|merge|ship))\b", re.IGNORECASE)


def decide(jev, text, offers, created, now, min_confidence=HIGH_CONFIDENCE):
    """What to do about the owner's message. {"action": "clear" | "ask" | "none", "pr", "why", "screen", "judge", "question", "shown"}.

    * no offers, or a message with no clearing words (`good to go`, `cleared`, `approved`, `ship it`, ...): `none`; the judge is not called;
    * the screen refuses (no number, several, not offered, ambiguous, hedged, too long, a message from before the offer, an old offer): the judge is asked only whether the message is about clearing at all:
      "nothing" at NOTHING_CONFIDENCE means `none` (the owner is not bothered), anything else is `ask`. A judge's high-confidence "clears it" changes nothing here;
    * the screen found ONE candidate: `clear` only when the judge picks exactly that PR at the high bar; "nothing" at its bar is `none`; every other
      answer (unsure, unreachable, another offer, several, asks) is `ask`.
    `shown` lists the PRs the question puts in front of the owner, for `show()` to move their shown heads."""
    checked = screen(text, offers, created, now)
    if checked.get("why") == "no_offers":
        return {"action": "none", "pr": None, "why": "no_offers", "screen": checked, "judge": None, "question": None, "shown": []}
    if not CLEARISH.search(text or ""):   # chat that has no clearing words in it is not asked about and costs no judge call
        return {"action": "none", "pr": None, "why": "no clearing words", "screen": checked, "judge": None, "question": None, "shown": []}
    verdict = judge(jev, text, offers, min_confidence)
    base = {"pr": None, "screen": checked, "judge": verdict, "question": None, "shown": []}
    probabilities = verdict.get("probabilities") or {}
    says_nothing = verdict.get("jev_choice") == OPT_NOTHING and probabilities.get(OPT_NOTHING, 0.0) >= NOTHING_CONFIDENCE
    if checked["verdict"] == "candidate":
        pr = checked["pr"]
        if verdict["confident"] and verdict["choice"] == pr:
            return dict(base, action="clear", pr=pr, why="confirmed")
        if verdict["confident"] and verdict["choice"] == OPT_NOTHING:
            return dict(base, action="none", why="not about clearing")
        why = "jev"
        prs = [pr]
    else:
        if says_nothing:
            return dict(base, action="none", why="not about clearing")
        why, prs = checked["why"], checked.get("prs") or None
    return dict(base, action="ask", why=why, question=question_text(why, offers, prs), shown=sorted(prs or offers)[:QUESTION_OFFERS_MAX])
