"""Cloud client: the sending end of a message or report through the cloud channel (bin/cloud_channel.py).

    send(sender, recipient, text, post, key=None, token_path=TOKEN_PATH) -> (ok, line)

`post(request)` is the caller's transport: it takes {"authorization": "Bearer <token>", "body": {...}} (the shape
cloud_channel.handle reads) and returns {"status": N, ...}. Nothing here touches the network.
The transport must be pinned to the channel endpoint and must not log the request (it holds the bearer token).

Rules:
    - The token is read from the projected token file at each try, capped at MAX_TOKEN_BYTES and limited to token
      characters. It is not kept after the call returns, never printed, never in an error line (a server reason that
      echoes it is redacted).
    - Retries are min(retries, MAX_RETRIES): six tries by default with waits of 0.5, 1, 2, 4 and 8 seconds, a
      window of RETRY_WINDOW_SECONDS (15.5), so a restart of the hub's pod shorter than that is bridged. Only on transport failure (status 0) and 5xx, with an injected sleep.
      Any other status is returned at once as a one-line error. A duplicate (200 with duplicate true) is delivered.
    - A key is made once per message (or given) and reused on every retry, so the channel delivers it once.
    - 401 and 403 are one-line errors that name the status and the channel's reason, with no token.
    - A report is a message to the boss the caller names, with the text prefixed "report: ".
"""
import re
import time
import uuid

TOKEN_PATH = "/var/run/secrets/tokens/fleet-switchboard"
RETRIES = 6
MAX_RETRIES = 6
MAX_TOKEN_BYTES = 4096
BACKOFF_SECONDS = 0.5
# Six tries, five waits of 0.5, 1, 2, 4 and 8 seconds: the stated retry window, in seconds (one place).
RETRY_WINDOW_SECONDS = sum(BACKOFF_SECONDS * (2 ** n) for n in range(MAX_RETRIES - 1))
_TOKEN_CHARS = re.compile(r"[A-Za-z0-9._~+/=-]+")


class _TokenError(Exception):
    pass


def _read_token(path):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_TOKEN_BYTES + 1)
    except OSError:
        raise _TokenError("cloud channel: no token file")
    if len(raw) > MAX_TOKEN_BYTES:
        raise _TokenError("cloud channel: token file over %d bytes" % MAX_TOKEN_BYTES)
    try:
        token = raw.decode("ascii").strip()
    except UnicodeError:
        raise _TokenError("cloud channel: token file has bad characters")
    if not token:
        raise _TokenError("cloud channel: empty token file")
    if not _TOKEN_CHARS.fullmatch(token):
        raise _TokenError("cloud channel: token file has bad characters")
    return token


def _clean(value, token=""):
    text = str(value)
    if token:
        text = text.replace(token, "?")
    return re.sub(r"[^\w .:,/-]", "?", text)[:80]


def _attempt(sender, recipient, text, key, post, token_path):
    """One try. Returns the answer; the request (and the token in it) is dropped when this returns."""
    token = _read_token(token_path)
    request = {"authorization": "Bearer " + token,
               "body": {"sender": sender, "recipient": recipient, "key": key, "text": text}}
    try:
        answer = post(request)
    except Exception as err:   # transport failed: the caller retries with the same key
        answer = {"status": 0, "error": type(err).__name__}
    if not isinstance(answer, dict):
        return {"status": "?", "error": ""}, token
    return answer, token


def send(sender, recipient, text, post, key=None, token_path=TOKEN_PATH, retries=RETRIES, sleep=time.sleep):
    """Post one message. Returns (True, "") on delivery or duplicate, else (False, one-line error)."""
    key = key or uuid.uuid4().hex
    tries = max(1, min(retries, MAX_RETRIES))
    status, reason = "?", ""
    for n in range(tries):
        try:
            answer, token = _attempt(sender, recipient, text, key, post, token_path)
        except _TokenError as err:
            return False, str(err)
        status, reason = answer.get("status"), _clean(answer.get("error", ""), token)
        del token, answer
        if status == 200:
            return True, ""
        if status != 0 and not (isinstance(status, int) and 500 <= status <= 599):
            return False, "cloud channel refused (%s): %s" % (_clean(status), reason)
        if n + 1 < tries:
            sleep(BACKOFF_SECONDS * (2 ** n))
    return False, "cloud channel unavailable (status %s: %s) after %d tries" % (_clean(status), reason, tries)


def report(sender, boss, text, post, key=None, token_path=TOKEN_PATH, retries=RETRIES, sleep=time.sleep):
    """Report to `boss`, the one the sender reports to (named by the caller, no default), through the same channel."""
    return send(sender, boss, "report: " + text, post, key, token_path, retries, sleep)
