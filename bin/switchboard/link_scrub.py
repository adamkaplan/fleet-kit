# ---- #1576: a URL in a GitHub comment summary that carries a signed name in its query or fragment loses both (docs/switchboard.md, "Signed links in comment summaries").

import re
import urllib.parse


SIGNED_NAMES = ("token", "access_token", "id_token", "refresh_token", "sig", "signature", "key", "api_key", "apikey", "secret",
                "client_secret", "password", "passwd", "code", "expires", "expiry", "x-amz-signature", "x-amz-credential",
                "x-amz-security-token", "auth", "jwt", "sid", "ticket", "sas", "pwd", "credential")
SIGNED_SUBSTRINGS = ("token", "secret", "password", "signature")
QUERY_REMOVED = "[query removed]"
_SCRUB_URL_RE = re.compile(r"(?i)https?(?::\/\/|%3A%2F%2F)[^\s<>\"'`]*")
_MARKS = "?#\uff1f\uff03"
_TRAILING_ESCAPE_RE = re.compile(r"%(?:25)*[0-9A-Fa-f]{2}$")


def _decode(text):
    for _ in range(3):
        again = urllib.parse.unquote(text)
        if again == text:
            break
        text = again
    return text.replace("\uff1f", "?").replace("\uff03", "#")


def _signed_name(name):
    lowered = _decode(name).strip().lower()
    return lowered in SIGNED_NAMES or any(part in lowered for part in SIGNED_SUBSTRINGS)


def _signed_part(part):
    return any(_signed_name(pair.split("=", 1)[0]) for pair in re.split(r"[&;?#]", part) if pair)


def _scrub_one_url(match):
    url = match.group(0)
    decoded = _decode(url)
    cut = [i for i in (decoded.find("?"), decoded.find("#")) if i >= 0]
    if not cut or not _signed_part(decoded[min(cut) + 1:]):
        return url
    head = url
    for i in range(1, len(url) + 1):
        if "?" in _decode(url[:i]) or "#" in _decode(url[:i]):
            head = url[:i]
            break
    found = _TRAILING_ESCAPE_RE.search(head)
    head = head[:found.start()] if found and head[-1] not in _MARKS else head[:-1]
    return head + QUERY_REMOVED


def scrub_signed_links(text):
    """`text` with, in every http or https URL run (up to whitespace, `<`, `>`, a quote or a backtick) whose names after the first `?` or `#` (read
    after up to 3 rounds of percent-decoding, U+FF1F as `?`, U+FF03 as `#`) hold a name of SIGNED_NAMES (or one containing a SIGNED_SUBSTRINGS
    word), everything from that first `?` or `#` replaced by `[query removed]`. Scheme, host, path and every other character stay."""
    if not isinstance(text, str):
        return ""
    if not any(c in text for c in "?#%\uff1f\uff03"):
        return text
    return _SCRUB_URL_RE.sub(_scrub_one_url, text)
