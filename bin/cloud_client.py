"""Cloud client: the sending end of a message or report through the cloud channel (bin/cloud_channel.py).

    send(sender, recipient, text, post, key=None, token_path=TOKEN_PATH) -> (ok, line)

`post(request)` is the caller's transport: it takes {"authorization": "Bearer <token>", "body": {...}} (the shape
cloud_channel.handle reads) and returns {"status": N, ...}. Nothing here touches the network.

Rules:
    - The token is read from the projected token file at each call, never kept in a variable that outlives the call,
      never printed, never in an error line.
    - A key is made once per message (or given) and reused on every retry, so the channel delivers it once.
    - 401 and 403 are one-line errors that name the status and the channel's reason, with no token.
    - A report is a message to the recipient "captain" with the text prefixed "report: ".
"""
import re
import uuid

TOKEN_PATH = "/var/run/secrets/tokens/fleet-switchboard"
RETRIES = 3
REPORT_TO = "captain"


def _read_token(path):
    with open(path, encoding="ascii") as handle:
        return handle.read().strip()


def _clean(value):
    return re.sub(r"[^\w .:,/-]", "?", str(value))[:80]


def _line(status, reason):
    return "cloud channel refused (%d): %s" % (status, _clean(reason))


def send(sender, recipient, text, post, key=None, token_path=TOKEN_PATH, retries=RETRIES):
    """Post one message. Returns (True, "") on delivery or duplicate, else (False, one-line error)."""
    key = key or uuid.uuid4().hex
    answer = None
    for _ in range(max(1, retries)):
        try:
            token = _read_token(token_path)
        except (OSError, UnicodeError):
            return False, "cloud channel: no token file"
        if not token:
            return False, "cloud channel: empty token file"
        request = {"authorization": "Bearer " + token,
                   "body": {"sender": sender, "recipient": recipient, "key": key, "text": text}}
        del token
        try:
            answer = post(request)
        except Exception as err:   # transport failed: retry with the same key
            answer = {"status": 0, "error": type(err).__name__}
        status = answer.get("status") if isinstance(answer, dict) else None
        if status == 200:
            return True, ""
        if status in (401, 403):
            return False, _line(status, answer.get("error", ""))
    status = answer.get("status") if isinstance(answer, dict) else "?"
    reason = answer.get("error", "") if isinstance(answer, dict) else ""
    return False, "cloud channel unavailable (status %s: %s) after %d tries" % (_clean(status), _clean(reason), max(1, retries))


def report(sender, text, post, key=None, token_path=TOKEN_PATH, retries=RETRIES):
    """Report to the captain through the same channel."""
    return send(sender, REPORT_TO, "report: " + text, post, key, token_path, retries)
