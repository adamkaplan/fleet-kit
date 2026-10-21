"""Cloud channel: the receiving end of a message between two cloud agents (slice 2 of the plan on #1059).

One function, `handle(request, reviewer, inbox, audit)`. No listener, no network and no daemon wiring: the caller passes in
everything that touches the outside, so the rules here can be driven by fakes.

    request   {"authorization": "Bearer <projected ServiceAccount token>",
               "body": {"sender": ..., "recipient": ..., "key": ..., "text": ...}}
    reviewer  reviewer(token, audience) -> a TokenReview status dict: {"authenticated": bool, "audiences": [...],
              "user": {"username": "system:serviceaccount:<namespace>:<name>"}}. The real one posts a TokenReview to
              the cluster; a token that expired fails there (authenticated false).
    inbox     inbox.put_if_absent(recipient, dedupe_key, item) -> bool: ONE atomic step that stores the item unless the
              recipient already holds that dedupe key, and says whether it stored it. A has-then-put pair would let two
              concurrent requests with one key both deliver, so the inbox (not this module) owns the atomicity.
    audit     audit(line): one string per refusal or delivery; it never holds a token.

Rules:
    - The sender is the agent the token proves (the ServiceAccount's name), never the name in the body. A body that
      names another sender is refused with 403 and one audit line.
    - A token that is missing, expired, not authenticated or not issued for this audience is refused with 401.
    - The ServiceAccount must be in the pinned namespace (default `fleet-agents`); a same-named account elsewhere is
      refused, and so is every call when the namespace is empty (fail closed).
    - A key already in the recipient's inbox is not delivered again (the same shape as the daemon's delivery dedupe:
      a key found in the inbox is never delivered twice); the repeat answers 200 with "duplicate": true. The key is
      scoped to the verified sender, so one agent cannot suppress another's message by using its key.
    - A failing reviewer refuses (503): nothing is let through when the token cannot be checked. A failing inbox also
      answers 503 with one audit line. The audit sink itself is the caller's: an exception from it propagates.
    - `text` is stored as received (bounded in length). Whatever later prints it must quote it and strip control
      characters; the key is restricted to printable ASCII here.
    - Names reserved by the system (a captain or switchboard account) are not refused here: the listener slice must
      reject them, or the namespace must hold only agent accounts.
"""
import re

AUDIENCE = "fleet-switchboard"
SERVICE_ACCOUNT_PREFIX = "system:serviceaccount:"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
NAMESPACE = "fleet-agents"
MAX_TOKEN = 4096
KEY = re.compile(r"[\x21-\x7e]{1,200}")   # printable ASCII, no spaces or control characters
MAX_TEXT = 20000
BEARER = "Bearer "


def _reply(status, **fields):
    fields["status"] = status
    return fields


def _refuse(audit, status, reason, **fields):
    audit("refused status=%d reason=%s%s" % (status, reason, "".join(" %s=%s" % (k, _clean(v)) for k, v in fields.items())))
    return _reply(status, error=reason)


def _clean(value):
    """A value fit for one audit line: no control characters, bounded."""
    return re.sub(r"[^\w.:@/-]", "?", str(value))[:80]


def agent_of(user, namespace):
    """The agent name a ServiceAccount username proves, or None."""
    if not isinstance(user, dict) or not isinstance(user.get("username"), str):
        return None
    username = user["username"]
    if not username.startswith(SERVICE_ACCOUNT_PREFIX):
        return None
    parts = username[len(SERVICE_ACCOUNT_PREFIX):].split(":")
    if len(parts) != 2 or not namespace or parts[0] != namespace or not NAME.fullmatch(parts[1]):
        return None
    return parts[1]


def handle(request, reviewer, inbox, audit, audience=AUDIENCE, namespace=NAMESPACE):
    """Verify the token, hold the claimed sender to it, deliver the keyed message once. Returns {"status": N, ...}."""
    header = request.get("authorization") if isinstance(request, dict) else None
    token = header[len(BEARER):].strip() if isinstance(header, str) and header.startswith(BEARER) else ""
    if not token:
        return _refuse(audit, 401, "no token")
    if len(token) > MAX_TOKEN:
        return _refuse(audit, 401, "token too long")
    try:
        review = reviewer(token, audience)
    except Exception as err:   # the cluster cannot be asked: closed, not open
        return _refuse(audit, 503, "token review unavailable", error=type(err).__name__)
    review = review if isinstance(review, dict) else {}
    if review.get("authenticated") is not True:
        return _refuse(audit, 401, "token not authenticated")
    audiences = review.get("audiences")
    if not isinstance(audiences, list) or audience not in audiences:
        return _refuse(audit, 401, "wrong audience")
    agent = agent_of(review.get("user"), namespace)
    if agent is None:
        return _refuse(audit, 401, "token is not an agent's")
    body = request.get("body")
    if not isinstance(body, dict):
        return _refuse(audit, 400, "no body")
    sender = body.get("sender")
    if sender != agent:
        return _refuse(audit, 403, "sender does not match token", token_agent=agent, claimed=sender)
    recipient, key, text = body.get("recipient"), body.get("key"), body.get("text")
    if not isinstance(recipient, str) or not NAME.fullmatch(recipient):
        return _refuse(audit, 400, "bad recipient")
    if not isinstance(key, str) or not KEY.fullmatch(key):
        return _refuse(audit, 400, "bad key")
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        return _refuse(audit, 400, "bad text")
    item = {"key": key, "sender": agent, "text": text}
    try:
        stored = inbox.put_if_absent(recipient, "%s/%s" % (agent, key), item)
    except Exception as err:   # delivery failed: say so, so the sender retries with the same key
        return _refuse(audit, 503, "inbox unavailable", error=type(err).__name__)
    if not stored:
        return _reply(200, delivered=False, duplicate=True)
    audit("delivered sender=%s recipient=%s key=%s" % (agent, recipient, _clean(key)))
    return _reply(200, delivered=True, duplicate=False)
