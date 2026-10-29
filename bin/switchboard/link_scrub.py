# ---- #1576: a URL in a GitHub comment summary that carries a signed name in its query or fragment loses both (docs/switchboard.md, "Signed links in comment summaries").

import re
import urllib.parse


SIGNED_NAMES = ("token", "access_token", "id_token", "refresh_token", "sig", "signature", "key", "api_key", "apikey", "secret",
                "client_secret", "password", "passwd", "code", "expires", "expiry", "x-amz-signature", "x-amz-credential",
                "x-amz-security-token", "auth", "jwt")
SIGNED_SUBSTRINGS = ("token", "secret", "password", "signature")
QUERY_REMOVED = "[query removed]"
_SCRUB_URL_RE = re.compile(r"(?i)(?:https?://|https?%3A%2F%2F)[^\s<>\"'`()\[\]{}|\\^]+")


def _signed_name(name):
    lowered = urllib.parse.unquote(name).strip().lower()
    return lowered in SIGNED_NAMES or any(part in lowered for part in SIGNED_SUBSTRINGS)


def _signed_part(part):
    return any(_signed_name(pair.split("=", 1)[0]) for pair in re.split(r"[&;]", part) if pair)


def _scrub_one_url(match):
    url = match.group(0)
    encoded = url[:3].lower() != "htt" or "://" not in url[:9]
    text = urllib.parse.unquote(url) if encoded else url
    cut = [i for i in (text.find("?"), text.find("#")) if i >= 0]
    if not cut:
        return url
    head, tail = text[:min(cut)], text[min(cut):]
    query, _, fragment = tail.partition("#")
    if not (_signed_part(query[1:]) or _signed_part(fragment)):
        return url
    return head + QUERY_REMOVED


def scrub_signed_links(text):
    """`text` with, in every URL whose query or fragment holds a name of SIGNED_NAMES (or a name containing a SIGNED_SUBSTRINGS word), both replaced by
    `[query removed]`. A percent-encoded URL is decoded first. Scheme, host, path and every other character stay."""
    if not isinstance(text, str) or ("?" not in text and "#" not in text and "%3F" not in text.upper() and "%23" not in text):
        return text if isinstance(text, str) else ""
    return _SCRUB_URL_RE.sub(_scrub_one_url, text)
