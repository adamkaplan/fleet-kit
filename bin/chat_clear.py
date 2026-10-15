"""Clearing a held PR by saying so in the owner's chat with the Chief of Staff (ask #457), slice 1: the offers and the deterministic checks.

This slice decides nothing and changes nothing on GitHub. It holds the three things the later slices stand on:

* the OFFERS: every open PR held for the owner, each at the head the owner was last shown. A hold is "owner-tier" when the PR carries the
  impact, credential or policy hold label or the captain marker label, however it was applied. A PR that also carries any OTHER hold label
  (security, contract, prod-config, other-area) is never offered: clearing it would lift only part of what holds it;
* the PERSON'S messages: a user message in the Chief of Staff's session WITHOUT fleet metadata, never one an agent wrote, new since the last
  read and recent;
* the CHECKS that no model can waive: the PR number must be written in the message and must be one of the offers. The later slices add the
  judge (it can only confirm or veto, never widen) and the clear itself.

A message that names no number, a number that is no offer, or several offers is a QUESTION for the owner, never a clear. The module reads and
writes only its own private file in the state directory and holds no credential. Python 3.9 standard library only.
"""
import json
import os
import re
import tempfile
from pathlib import Path

OFFERS_FILE = "chat-clear-offers.json"
OFFERS_MAX_BYTES = 262144
OFFERS_MAX = 500
HOLD_KEYS = ("hold-impact", "hold-credential", "hold-policy")     # the owner-tier hold labels (config-independent keys; the names get the user prefix)
CAPTAIN_KEY = "merge-captain"
OTHER_HOLD_KEYS = ("hold-security", "hold-contract", "hold-prod-config", "hold-other-area")
MESSAGE_MAX_AGE = 900          # seconds: an older message is not acted on
SUMMARY_MAX = 200
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
    present = set(_label_names(labels))
    if any(names[key] in present for key in OTHER_HOLD_KEYS if key in names):
        return False
    return bool(owner_tier_labels(names, labels))


def read_offers(state_dir):
    """{"owner/repo#N": {"head", "shown_at", "labels", "summary"}}, {} when there is no file. An unreadable or oversized file is an empty
    set: nothing is offered, so nothing can be cleared (it fails toward hold)."""
    path = Path(state_dir) / OFFERS_FILE
    try:
        if path.stat().st_size > OFFERS_MAX_BYTES:
            return {}
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    offers = data.get("offers") if isinstance(data, dict) else None
    if not isinstance(offers, dict):
        return {}
    good = {}
    for key, offer in offers.items():
        if isinstance(key, str) and re.match(r"^[^/#\s]+/[^/#\s]+#\d{1,9}$", key) and isinstance(offer, dict) \
                and isinstance(offer.get("head"), str) and offer["head"]:
            good[key] = offer
    return good


def write_offers(state_dir, offers):
    """Atomic, private (0600): a temporary file in the same directory, renamed over the old one."""
    directory = Path(state_dir)
    fd, tmp = tempfile.mkstemp(prefix=OFFERS_FILE + ".", dir=str(directory))
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"offers": offers}, handle, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(directory / OFFERS_FILE))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def refresh_offers(state_dir, holds, names, now):
    """Bring the offers to what is held now. `holds` is a list of {"pr": "owner/repo#N", "head", "labels", "summary"} read from the open PRs.

    * a held PR that is offerable and has no offer is recorded at its CURRENT head, with `shown_at` = now;
    * a PR that already has an offer KEEPS the head the owner was shown: a push after it is a mismatch the later slices put to the owner, and
      `show()` moves the shown head only when the new one has been put in front of him;
    * a PR that is no longer held (or no longer offerable) loses its offer.
    Returns {"offers", "added", "dropped"}; the file is written only when something changed."""
    before = read_offers(state_dir)
    after, added = {}, []
    for hold in holds or []:
        pr, head = hold.get("pr"), hold.get("head")
        if not isinstance(pr, str) or not isinstance(head, str) or not head or not is_offerable(names, hold.get("labels")):
            continue
        if len(after) >= OFFERS_MAX:
            break
        kept = before.get(pr)
        labels = owner_tier_labels(names, hold.get("labels"))
        summary = " ".join(str(hold.get("summary") or "").split())[:SUMMARY_MAX]
        if kept is None:
            after[pr] = {"head": head, "shown_at": int(now), "labels": labels, "summary": summary}
            added.append(pr)
        else:
            after[pr] = dict(kept, labels=labels, summary=summary or kept.get("summary", ""))
    dropped = sorted(set(before) - set(after))
    if after != before:
        write_offers(state_dir, after)
    return {"offers": after, "added": added, "dropped": dropped}


def show(state_dir, pr, head, now):
    """The owner has just been shown `pr` at `head` (a question that names it): the offer's shown head moves to it. False when `pr` has no offer."""
    offers = read_offers(state_dir)
    if pr not in offers or not isinstance(head, str) or not head:
        return False
    offers[pr] = dict(offers[pr], head=head, shown_at=int(now))
    write_offers(state_dir, offers)
    return True


def _has_fleet_meta(message):
    meta = message.get("metadata")
    if meta is None and isinstance(message.get("payload"), dict):
        meta = message["payload"].get("metadata")
    return isinstance(meta, dict) and isinstance(meta.get("fleet"), dict)


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
    [{"id", "text"}]. A message with fleet metadata is an agent's and is never returned, whatever it says; one without an id, already in `seen`, with no
    readable time or older than `max_age` seconds is skipped. `created_of(message)` gives the epoch seconds of a message."""
    out = []
    for message in prompts or []:
        if not isinstance(message, dict) or _has_fleet_meta(message):
            continue
        message_id = message.get("id")
        created = created_of(message)
        if not isinstance(message_id, str) or message_id in seen or created is None or now - created > max_age:
            continue
        out.append((created, {"id": message_id, "text": _text(message)}))
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
    """True only when the head the owner was shown is the PR's head now. A push after the offer clears nothing."""
    return isinstance(offer, dict) and isinstance(current_head, str) and bool(current_head) and offer.get("head") == current_head
