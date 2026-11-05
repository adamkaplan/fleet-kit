# ------------------------------------------------------- standing authority

_STANDING_AUTHORITY = re.compile(r"^(?:[-*]\s+)?\**standing authority\**\s*:\s*\**\s*(.*?)\**\s*$", re.IGNORECASE)


_ORDERS_HEADING = re.compile(r"^#{1,6}\s+standing orders\s*$", re.IGNORECASE)


_ANY_HEADING = re.compile(r"^#{1,6}\s")


_ORDER_BULLET = re.compile(r"^[-*]\s+(\S.*?)\s*$")


def _legacy_authority(body):
    """The charter's one-line `Standing authority:` text, or None when it states none.

    It lives in the charter issue's prose, below the yaml block; fenced code is
    ignored, the first such line wins, and "none" counts as none stated.
    """
    fenced = False
    for line in (body or "").splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        match = None if fenced else _STANDING_AUTHORITY.match(line.strip())
        if match:
            text = match.group(1).strip()
            return None if not text or text.lower().rstrip(".") in ("none", "none stated", "n/a") else text
    return None


def _orders_section(body):
    """The first `## Standing orders` section outside a fenced block: None, or a dict with the character
    offsets `start` (its heading), `head_end`, `end` (the next heading, or the end of the body) and
    `bullets`, a list of (start, end, text), one per order, in the body's own bytes."""
    body = body or ""
    fenced, offset, found = False, 0, None
    for line in body.splitlines(keepends=True):
        start, offset = offset, offset + len(line)
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        if found is None:
            if _ORDERS_HEADING.match(stripped):
                found = {"start": start, "head_end": offset, "end": len(body), "bullets": []}
            continue
        if _ANY_HEADING.match(stripped):
            found["end"] = start
            break
        match = _ORDER_BULLET.match(stripped)
        if match:
            found["bullets"].append((start, offset, match.group(1)))
    return found


def section_orders(body):
    """The orders of the `## Standing orders` section alone, in order."""
    found = _orders_section(body)
    return [text for _, _, text in found["bullets"]] if found else []


def parse_orders(body):
    """Every standing order the charter body states: the section's bullets in order, then the legacy
    `Standing authority:` line when there is one. [] when there are none."""
    legacy = _legacy_authority(body)
    return section_orders(body) + ([legacy] if legacy else [])


def fetch_charter_body(reader, charter, repo):
    """The charter issue's body (or None) through the reader's GET-only `gh api`: the one read of it."""
    data = reader._api("repos/%s/issues/%d" % (repo, charter), repo)
    return data.get("body") if isinstance(data, dict) and isinstance(data.get("body"), str) else None


def fetch_orders(reader, charter, repo):
    """The charter issue's standing orders (a list) through the reader's GET-only `gh api`."""
    return parse_orders(fetch_charter_body(reader, charter, repo))
