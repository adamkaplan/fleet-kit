# The state line of a cloud worker (ask #1397): the closed vocabulary of the error line, its redaction, and the reading and the formatting of the state.
# A part of the switchboard's namespace (bin/fleet-switchboard.parts); bin/cloud_worker.py wires it (its command entry points). A function that needs
# something of the cloud module takes that module as `W` (its first argument), so a name a test swaps in the module is seen here too.
import datetime as _datetime
import os
import re
import unicodedata

_SECRET_ENV_NAME = re.compile(r"(?:KEY|TOKEN|SECRET|PASS|CRED|AUTH)", re.I)
ERROR_LINE_MAX = 160
ERROR_TEXT_MAX = 2000000   # past this the text is not read at all (it prints one fixed phrase): the redaction below is linear, this only bounds its memory
_BREAK_CHARS = frozenset("\n\r\x0b\x0c\x85\u2028\u2029")   # every kind of line break: LF, CR, VT, FF, NEL, LS, PS

# THE closed vocabulary of the error line (ask #1397): the ONE list, in this ONE place. A token of the error line prints only if its lower-case form is
# here (or it is a short number, a separator or an error-class name, see redact_error). Short, technical and public: no names, no everyday nouns. The one
# inherent limit: a secret that is itself one of these words prints.
ERROR_VOCABULARY = frozenset("""
error errors failed failure fatal refused denied forbidden unauthorized timeout timed out expired invalid missing unknown unavailable not found
cannot could unable no such file directory exists exist permission access connection reset closed broken pipe exit exited code status signal killed
terminated limit exceeded quota memory disk space full retry retrying back off pull push image registry manifest layer pod pods container node cluster
namespace service account token secret key password session message request requests response server client agent user git branch commit merge conflict
rejected protected hook guard rule run runs running start started stopped crash crashed panic trace traceback line module import named desc rpc http
https tcp tls dns host port address path name value type expected got while when during after before on in at of to from for with without by as is
are was were be been the a an and or but if then else this that it its has have had did does do will would should may might can must all any some
only more than too many much one two first last again already now never always bad gateway unreachable unsupported unexpected internal required
authentication login open read write create delete update list get find warning exception notfound alreadyexists errimagepull imagepullbackoff
crashloopbackoff oomkilled evicted enoent eacces eexist econnrefused etimedout
runtime attribute os io assertion system called process subprocess url ssl json decode unicode index
""".split())
_SEPARATORS = frozenset(" :,.()\"'/=-_[]")   # the punctuation of an error sentence
_ERROR_CLASS = re.compile(r"[A-Z][A-Za-z]{0,39}")
_ERROR_CLASS_END = ("Error", "Exception", "Warning", "Timeout")
_CAMEL_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")   # a CamelCase word: split at lower-to-upper; a run of capitals is one word (HTTPError: HTTP, Error)
_TOKEN = re.compile(r"[^\W_]+|.", re.S)   # a run of letters and digits (any script), or one other character
PLACEHOLDER = "[redacted]"


def _normalise(text, spaced):
    """(N, idx): the text as the rules read it, and for each character of N the index of the original character it came from. NFKC; a line break or any
    white space is a plain space; a zero-width, format (bidi included), control or combining character is removed (`spaced` False: a word split by
    it is whole again) or turned into a space (`spaced` True: two words it separated stay two)."""
    chars, idx = [], []
    for i, ch in enumerate(text):
        if " " <= ch <= "~":
            piece = ch
        elif ch in _BREAK_CHARS or ch.isspace():
            piece = " "
        elif unicodedata.category(ch)[0] in "CM":
            piece = " " if spaced else ""
        else:
            piece = unicodedata.normalize("NFKC", ch)
        for one in piece:
            chars.append(" " if one.isspace() else one)
            idx.append(i)
    return "".join(chars), idx


def _token_allowed(token, before, after, known):
    """True when a run of letters and digits may print: a word of ERROR_VOCABULARY (compared in lower case), a number of at most five digits that stands
    between separators, or an error-class name (CamelCase letters only, at most 40, ending in Error, Exception, Warning or Timeout, and EVERY CamelCase word of it a word of ERROR_VOCABULARY)."""
    low = token.lower()
    if token.isascii():
        if token.isdigit():
            ok = len(token) <= 5 and before in _SEPARATORS and after in _SEPARATORS
        else:
            ok = low in ERROR_VOCABULARY or (token.isalpha() and _ERROR_CLASS.fullmatch(token) is not None and token.endswith(_ERROR_CLASS_END)
                                             and all(w.lower() in ERROR_VOCABULARY for w in _CAMEL_WORD.findall(token)))
    else:
        ok = False
    if ok and known and len(low) >= 4 and any(low in secret for secret in known):
        return False   # this run's own secret holds it: it stays out even when it is a word of the list
    return ok


def _mark_variant(line, spaced, known, mask):
    """Judge every token of one reading of the line and mark the ORIGINAL positions of each token that may not print (and of every character that is
    neither a letter, a digit, white space nor a separator)."""
    N, idx = _normalise(line, spaced)
    size = len(N)
    for found in _TOKEN.finditer(N):
        start, end = found.span()
        word = found.group()
        if len(word) == 1 and not word.isalnum():
            allowed = word in _SEPARATORS or word == " "
        else:
            before = N[start - 1] if start else " "
            after = N[end] if end < size else " "
            allowed = _token_allowed(word, before, after, known)
        if not allowed:
            low, high = idx[start], idx[end - 1] + 1
            mask[low:high] = b"\x01" * (high - low)


def redact_error(text, known=()):
    """The error line that `status` prints, through an ALLOW-LIST: leaks are excluded by construction. The LAST original line (any kind of line break
    ends a line; one that holds only invisible characters is empty) is read as tokens, in two readings (zero-width, format, control and combining
    characters turned into spaces, and removed; text normalised NFKC, white space one space); a token prints only when it is allowed in BOTH readings:
    a word of ERROR_VOCABULARY (lower-case comparison), a number of at most five digits between separators, or an error-class name; a separator
    (space : , . ( ) quotes / = - _ and brackets) prints. EVERY other token is `[redacted]`, whatever precedes it, in any language or spelling; a run of
    placeholders and the separators between them is one placeholder. The result is cut at ERROR_LINE_MAX. The one inherent limit: a secret that is
    itself a word of the vocabulary (or a number of up to five digits) prints; `known` (this run's own secrets) are kept out even then."""
    text = str(text or "")
    if not text.strip():
        return ""
    if len(text) > ERROR_TEXT_MAX:
        return "[error text too long to show]"
    line = None
    for part in reversed(re.split("[\n\r\x0b\x0c\x85\u2028\u2029]", text)):   # the last line that holds a visible character
        if _normalise(part, True)[0].strip():
            line = part
            break
    if line is None:
        return ""
    forms = [k.lower() for k in known if k and len(k.strip()) >= 4]
    mask = bytearray(len(line))
    for spaced in (False, True):
        _mark_variant(line, spaced, forms, mask)
    out = []   # items: (kind, text) with kind "s" separator, "w" word, "r" placeholder
    for i, ch in enumerate(line):
        if len(out) > 2000:   # far more than ERROR_LINE_MAX shows
            break
        if mask[i]:
            if out and out[-1][0] == "r":
                continue
            seps = 0
            while seps < len(out) and out[-1 - seps][0] == "s":
                seps += 1
            if seps < len(out) and out[-1 - seps][0] == "r":   # placeholder, separators, placeholder: one placeholder
                del out[len(out) - seps:]
                continue
            out.append(("r", PLACEHOLDER))
            continue
        if ch in _SEPARATORS or ch.isspace() or unicodedata.category(ch)[0] in "CM":
            piece = ch if ch in _SEPARATORS else " "
            if piece == " " and out and out[-1] == ("s", " "):
                continue
            out.append(("s", piece))
        else:
            out.append(("w", ch))
    return "".join(piece for _, piece in out).strip()[:ERROR_LINE_MAX]



def known_secrets(W, state, record, cluster):
    """The values that must never print: this command's own (the cluster login's), the worker's password (and its other forms), and the values of
    this process's environment variables whose name says key, token, secret, pass, credential or auth."""
    known = set(getattr(cluster, "known", ()) or ())
    try:
        password = W._read_private(W.password_path(state, record["worker"]), W.PASSWORD_MAX + 2).strip()
        if password:
            known.update(W._lib().secret_forms(password))
    except (OSError, ValueError, W.CloudError):
        pass
    known.update(v for k, v in os.environ.items() if v and len(v.strip()) >= 8 and _SECRET_ENV_NAME.search(k))
    return known


def _message_ms(message):
    times = message.get("time") if isinstance(message.get("time"), dict) else {}
    found = [times.get(k) for k in ("completed", "created") if isinstance(times.get(k), (int, float)) and not isinstance(times.get(k), bool)]
    return max(found) if found else None


def _message_error(W, message):
    """The last error line of one message, or None: its `error` field (a string, or an object's message or name), else the last line of its text that
    says error, failed or exception."""
    err = message.get("error")
    if isinstance(err, dict):
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        err = err.get("message") or data.get("message") or err.get("name")
    if isinstance(err, str) and err.strip():
        return err
    for line in reversed(str(W.H["_message_text"](message)).splitlines()):
        if re.search(r"\b(error|failed|exception|traceback)\b", line, re.I):
            return line
    return None


def _read_agent(W, v2, session, messages=20):
    """(waiting, newest message time in ms or None, the newest error text or None), read from the agent service of the pod: the session's pending
    permission requests and questions (forms), and, when `messages` is above 0, its newest messages. Reads only."""
    waiting = bool(v2.permission_list(session)) or bool(v2.form_list(session))
    found = [m for m in ((v2.message_list(session, limit=messages, order="desc") or []) if messages else []) if isinstance(m, dict)]
    times = [t for t in (_message_ms(m) for m in found) if t is not None]
    error = next((e for e in (_message_error(W, m) for m in found) if e), None)
    return waiting, (max(times) if times else None), error


def _when(ms):
    if ms is None:
        return "none yet"
    try:
        return _datetime.datetime.fromtimestamp(ms / 1000.0, _datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return "unreadable"


def state_line(name, state, row):
    """THE one line: the state, the time of the last tool call or message, and the last error line (already redacted and cut)."""
    return "cloud %s: %s; last tool call or message %s; last error %s" % (name, row["state"], _when(row.get("last_ms")), row.get("error") or "none")


def pod_read_state(W, runner, state, cluster, record, timeout, snapshot=None, v2_for=None, messages=20):
    """{state, last_ms, error}: starting (the pod is Pending, or Running with no session yet), running (a session and no pending prompt), waiting on a
    prompt (a pending permission request or question on the session) or exited (the pod is Succeeded or Failed, or gone). A pod that cannot be told
    or read raises W.CloudError: it is never called running. READ ONLY: no message is sent and nothing is written."""
    if not W._pod_known(record):
        raise W.CloudError("the runtime id is not pod/<name>: the pod cannot be told")
    pod = W._pod_of(record, snapshot if snapshot is not None else W._cluster_snapshot(runner, cluster, timeout))
    phase = W._safe(pod.get("phase")) if pod else None
    row = {"state": "exited", "last_ms": None, "error": None}
    if pod is None or phase in ("Succeeded", "Failed"):
        return row
    if phase == "Pending" or not record.get("session"):
        row["state"] = "starting"
        return row
    if phase != "Running":
        raise W.CloudError("the pod phase is %s" % phase)
    try:
        if v2_for is not None:
            waiting, last, error = _read_agent(W, v2_for(), record["session"], messages)
        else:
            with W.tunnel(cluster, record) as live:
                waiting, last, error = _read_agent(W, W.remote_v2(state, live, timeout), record["session"])
    except (W.H["V2Unreachable"], OSError) as err:
        raise W.ServiceSilent("the agent service did not answer (%s)" % type(err).__name__) from None
    except W.H["V2Error"] as err:
        if err.reason in ("timeout", "unavailable", "backoff"):   # no answer in time: the same as silence
            raise W.ServiceSilent("the agent service did not answer (%s)" % err.reason) from None
        raise W.CloudError("the agent service refused the read (%s)" % err.reason) from None
    row.update(state="waiting on a prompt" if waiting else "running", last_ms=last,
               error=W.redact_error(error, W._known_secrets(state, record, cluster)) if error else None)
    return row



def pod_launch_state_line(W, runner, state, cluster, record, name, v2, accepted_ms=None, seconds=None):
    """The line `launch --cloud` prints ONCE, after the pod's address is known and its brief was accepted (`accepted_ms`: when the first message of
    the session was accepted, which is then the time of the last message; the message log itself is not read here). The state read is tried again
    every 2 s for at most `seconds` (by `_clock`); past that it returns `no state read within N s`. Never raises (but an interrupt): the launch
    never fails because of this wait. Read only."""
    seconds = W.STATE_WAIT_SECONDS if seconds is None else seconds
    end = W._clock() + seconds
    try:   # the pod phase is read ONCE (a cluster read); only the agent service is asked again
        snapshot = W._cluster_snapshot(runner, cluster, min(W.STATUS_SECONDS, seconds))
    except Exception as err:
        return "cloud %s: %s (%s)" % (name, W.READ_FAIL, W._one(cluster.clean(str(err), 100), 100))
    while True:
        try:
            row = W.read_state(runner, state, cluster, record, W.STATUS_SECONDS, snapshot=snapshot, v2_for=lambda: v2, messages=0)
            row["last_ms"] = row["last_ms"] or accepted_ms
            return W.state_line(name, state, row)
        except W.ServiceSilent:   # the service does not answer yet: try again until the bound
            pass
        except Exception as err:   # an answer that is a refusal, or a pod that cannot be told: said now, not waited for
            return "cloud %s: %s (%s)" % (name, W.READ_FAIL, W._one(cluster.clean(str(err), 100), 100))
        if W._clock() >= end:
            return "cloud %s: no state read within %d s" % (name, seconds)
        W._sleep(min(2, max(0.0, end - W._clock())))

