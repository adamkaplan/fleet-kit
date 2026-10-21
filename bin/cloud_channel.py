"""Cloud channel: the receiving end of a message between two cloud agents (slice 2 of the plan on #1059).

One function, `handle(request, reviewer, inbox, audit)`. No listener, no network and no daemon wiring: the caller passes in
everything that touches the outside, so the rules here can be driven by fakes.

    request   {"authorization": "Bearer <projected ServiceAccount token>",
               "body": {"sender": ..., "recipient": ..., "key": ..., "text": ...}}
    reviewer  reviewer(token, audience) -> a TokenReview status dict: {"authenticated": bool, "audiences": [...],
              "user": {"username": "system:serviceaccount:<namespace>:<name>"}}. The real one posts a TokenReview to
              the cluster; a token that expired fails there (authenticated false).
    inbox     inbox.has(recipient, key) -> bool;  inbox.put(recipient, item)
    audit     audit(line): one string per refusal; it never holds a token.

Rules:
    - The sender is the agent the token proves (the ServiceAccount's name), never the name in the body. A body that
      names another sender is refused with 403 and one audit line.
    - A token that is missing, expired, not authenticated or not issued for this audience is refused with 401.
    - A key already in the recipient's inbox is not delivered again (the same shape as the daemon's delivery dedupe:
      a key found in the inbox is never delivered twice); the repeat answers 200 with "duplicate": true.
    - A failing reviewer refuses (503): nothing is let through when the token cannot be checked.
"""
import re

AUDIENCE = "fleet-switchboard"
SERVICE_ACCOUNT_PREFIX = "system:serviceaccount:"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_KEY = 200
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


def agent_of(user, namespace=None):
    """The agent name a ServiceAccount username proves, or None."""
    if not isinstance(user, dict) or not isinstance(user.get("username"), str):
        return None
    username = user["username"]
    if not username.startswith(SERVICE_ACCOUNT_PREFIX):
        return None
    parts = username[len(SERVICE_ACCOUNT_PREFIX):].split(":")
    if len(parts) != 2 or (namespace is not None and parts[0] != namespace) or not NAME.match(parts[1]):
        return None
    return parts[1]


def handle(request, reviewer, inbox, audit, audience=AUDIENCE, namespace=None):
    """Verify the token, hold the claimed sender to it, deliver the keyed message once. Returns {"status": N, ...}."""
    header = request.get("authorization") if isinstance(request, dict) else None
    token = header[len(BEARER):].strip() if isinstance(header, str) and header.startswith(BEARER) else ""
    if not token:
        return _refuse(audit, 401, "no token")
    try:
        review = reviewer(token, audience)
    except Exception:   # the cluster cannot be asked: closed, not open
        return _refuse(audit, 503, "token review unavailable")
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
        return _reply(400, error="no body")
    sender = body.get("sender")
    if sender != agent:
        return _refuse(audit, 403, "sender does not match token", token_agent=agent, claimed=sender)
    recipient, key, text = body.get("recipient"), body.get("key"), body.get("text")
    if not isinstance(recipient, str) or not NAME.match(recipient):
        return _reply(400, error="bad recipient")
    if not isinstance(key, str) or not key or len(key) > MAX_KEY:
        return _reply(400, error="bad key")
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        return _reply(400, error="bad text")
    if inbox.has(recipient, key):
        return _reply(200, delivered=False, duplicate=True)
    inbox.put(recipient, {"key": key, "sender": agent, "text": text})
    return _reply(200, delivered=True, duplicate=False)
