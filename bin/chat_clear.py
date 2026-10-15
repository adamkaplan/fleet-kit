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
        if not (isinstance(key, str) and re.match(r"^[^/#\s]+/[^/#\s]+#[0-9]{1,9}$", key)):
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


def _clean(text, limit):
    return " ".join(CONTROL.sub(" ", str(text or "")).split())[:limit].encode("utf-8", "replace").decode("utf-8")   # a lone surrogate cannot be written: it becomes ?


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


def screen(text, offers, created, now):
    """The checks no model can waive. Returns {"verdict": "candidate", "pr"} when the message names exactly ONE number, that number stands alone, exactly one
    offer has it and the message came AFTER the owner was shown that offer, else {"verdict": "question", "why": <code>, "prs": [...]} (the owner is asked,
    nothing is cleared):

    * `no_offers`: nothing is held for the owner;
    * `no_number`: the message names no number (the number is required, whatever a judge would say);
    * `several_numbers`: it has more than one DISTINCT run of digits, however they are joined (`366-367`, `366/367`, `366, 389`): one message clears one PR;
    * `unclear_number`: its one number sits inside a word or a decimal (`abc366`, `3.66`);
    * `not_offered`: the number is not one of the offers;
    * `ambiguous_number`: more than one offer has that number (the same number in two repositories);
    * `no_time`, `before_offer`, `expired`: see `timing_problem` (`created` is the message's time, `now` the clock)."""
    prs = sorted(offers or {})
    if not prs:
        return {"verdict": "question", "why": "no_offers", "prs": []}
    numbers = numbers_in(text)
    if not numbers:
        return {"verdict": "question", "why": "no_number", "prs": prs}
    if len(numbers) > 1:
        return {"verdict": "question", "why": "several_numbers", "prs": prs}
    if not _standalone(text, numbers[0]):
        return {"verdict": "question", "why": "unclear_number", "prs": prs}
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
