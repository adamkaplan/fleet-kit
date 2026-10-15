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
PR_NUMBER = re.compile(r"(?<![\w.#/-])#?(\d{1,6})(?![\w]|\.\d)")


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


def _load(state_dir):
    """(offers, problem). `problem` is None for a good file or no file, else a short reason: an unreadable, oversized or malformed file offers nothing."""
    path = Path(state_dir) / OFFERS_FILE
    try:
        if path.stat().st_size > OFFERS_MAX_BYTES:
            return {}, "the offers file is larger than %d bytes" % OFFERS_MAX_BYTES
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as err:
        return {}, "the offers file cannot be read (%s)" % type(err).__name__
    offers = data.get("offers") if isinstance(data, dict) else None
    if not isinstance(offers, dict):
        return {}, "the offers file is not an object of offers"
    good = {}
    for key, offer in offers.items():
        if len(good) >= OFFERS_MAX:
            break
        if isinstance(key, str) and re.match(r"^[^/#\s]+/[^/#\s]+#\d{1,9}$", key) and isinstance(offer, dict) \
                and isinstance(offer.get("head"), str) and offer["head"]:
            good[key] = offer
    return good, None


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
    Returns {"offers", "added", "dropped", "problem", "quarantined", "capped"}; the file is written only when something changed.
    Raises OffersBusy when the lock wait runs out."""
    with _locked(state_dir, lock_seconds):
        before, problem = _load(state_dir)
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
                after[pr] = {"head": hold["head"], "shown": not problem, "shown_at": int(now), "labels": labels, "summary": summary}
                added.append(pr)
            else:
                after[pr] = dict(kept, labels=labels, summary=summary or kept.get("summary", ""))
        dropped = sorted(set(before) - set(after))
        if after != before:
            write_offers(state_dir, after)
    return {"offers": after, "added": added, "dropped": dropped, "problem": problem, "quarantined": quarantined, "capped": capped}


def show(state_dir, pr, head, now, lock_seconds=LOCK_SECONDS):
    """The owner has just been shown `pr` at `head` (a question that names it): the offer's shown head moves to it. False when `pr` has no offer.
    Raises OffersBusy when the lock wait runs out."""
    with _locked(state_dir, lock_seconds):
        offers = read_offers(state_dir)
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


def _text(message):
    text = message.get("text")
    if isinstance(text, str):
        return text
    payload = message.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("text"), str):
        return payload["text"]
    parts = message.get("content")
    if isinstance(parts, list):
        return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def person_messages(prompts, seen, now, created_of, max_age=MESSAGE_MAX_AGE):
    """The person's own NEW messages out of `prompts` (a Chief of Staff session's user messages, as the engine read them), oldest first, as
    [{"id", "text"}]. A message with fleet metadata (or metadata that cannot be read as none) is an agent's and is never returned, whatever it says; one
    without an id, already in `seen`, with no readable time, older than `max_age` seconds or dated more than MESSAGE_SKEW seconds ahead is skipped. At most
    MESSAGES_SCANNED prompts are looked at and TEXT_MAX characters of each kept. `created_of(message)` gives the epoch seconds of a message."""
    out = []
    for message in list(prompts or [])[:MESSAGES_SCANNED]:
        if not isinstance(message, dict) or _is_agents(message):
            continue
        message_id = message.get("id")
        created = created_of(message)
        if isinstance(created, bool) or not isinstance(created, (int, float)) or created != created:
            continue   # no readable time, or not a number: skipped, never an error
        if not isinstance(message_id, str) or message_id in seen or now - created > max_age or created - now > MESSAGE_SKEW:
            continue
        out.append((created, {"id": message_id, "text": _text(message)[:TEXT_MAX]}))
    return [item for _, item in sorted(out, key=lambda row: row[0])]


def numbers_in(text):
    """The PR numbers written in `text`, in order, once each: `366`, `#366`; not part of a longer word, a decimal or a path."""
    seen = []
    for found in PR_NUMBER.finditer(text or ""):
        number = int(found.group(1))
        if number not in seen:
            seen.append(number)
    return seen


def screen(text, offers):
    """The checks no model can waive. Returns {"verdict": "candidate", "pr"} when the message names exactly ONE number and exactly one offer
    has it, else {"verdict": "question", "why": <code>, "prs": [...]} (the owner is asked, nothing is cleared):

    * `no_offers`: nothing is held for the owner;
    * `no_number`: the message names no PR number (the number is required, whatever a judge would say);
    * `several_numbers`: it names more than one number: one message clears one PR;
    * `not_offered`: the number is not one of the offers;
    * `ambiguous_number`: more than one offer has that number (the same number in two repositories)."""
    prs = sorted(offers or {})
    if not prs:
        return {"verdict": "question", "why": "no_offers", "prs": []}
    numbers = numbers_in(text)
    if not numbers:
        return {"verdict": "question", "why": "no_number", "prs": prs}
    if len(numbers) > 1:
        return {"verdict": "question", "why": "several_numbers", "prs": prs}
    matches = [pr for pr in prs if int(pr.rsplit("#", 1)[1]) == numbers[0]]
    if not matches:
        return {"verdict": "question", "why": "not_offered", "prs": prs}
    if len(matches) > 1:
        return {"verdict": "question", "why": "ambiguous_number", "prs": matches}
    return {"verdict": "candidate", "pr": matches[0]}


def head_matches(offer, current_head):
    """True only when the head the owner was shown is the PR's head now. A push after the offer clears nothing, and neither does an offer
    that was recorded after the offers were lost and has not been shown (`shown: false`)."""
    return isinstance(offer, dict) and offer.get("shown") is True and isinstance(current_head, str) and bool(current_head) \
        and offer.get("head") == current_head
