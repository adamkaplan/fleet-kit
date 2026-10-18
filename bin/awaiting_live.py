"""The age cap on the `awaiting-live` exemption (#576): triage skips an issue labelled `<user>:awaiting-live` (#574), but not for ever.

The label's age and the comments since are read from the issue's timeline (one bounded page per labelled issue, cached by the caller). An issue whose label is
older than `awaiting_live_days` and that has no comment newer than the label comes back as a T9 finding worded `awaiting-live expired`; when the age cannot be
read it comes back as `awaiting-live age unknown`: the fail direction is VISIBLE, because the label exists to hide an issue and a guess of "fresh" would hide
it for ever. Pure functions: no read happens here."""

SUFFIX = ":awaiting-live"
CONFIG_KEY = "awaiting_live_days"   # the module declares its own configuration key (bin/fleet-switchboard config_declarations)
CONFIG_DEFAULT = 14
MAX_DAYS = 365
EXPIRED = "awaiting-live expired"
UNKNOWN = "awaiting-live age unknown"


def validate(raw, source):
    """Raises ValueError naming the key: a number of days, more than 0 and at most 365 (a bool is not a number here)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 < raw <= MAX_DAYS:
        raise ValueError("%s: '%s' must be a number of days above 0 and at most %d, got %r" % (source, CONFIG_KEY, MAX_DAYS, raw))


def days_of(config):
    """The configured limit in days, the default when the key is absent or unusable."""
    raw = (config or {}).get(CONFIG_KEY, CONFIG_DEFAULT)
    return raw if not isinstance(raw, bool) and isinstance(raw, (int, float)) and 0 < raw <= MAX_DAYS else CONFIG_DEFAULT


def label_names(labels):
    """The labels of an issue that end in the suffix."""
    return {str(l) for l in labels if str(l).endswith(SUFFIX)}


def read(events, names, parse):
    """From one page of timeline events: {"set", "event_id", "commented"} (epoch seconds; `commented` is the newest comment, None when there is none after the
    page's start), or None when no `labeled` event of one of `names` is there. `parse` turns a timestamp into epoch seconds (None when it is not one)."""
    set_at, event_id, commented = None, None, None
    for event in events:
        if not isinstance(event, dict):
            continue
        at = parse(event.get("created_at"))
        if at is None:
            continue
        kind = event.get("event")
        if kind == "labeled" and isinstance((event.get("label") or {}).get("name"), str) and event["label"]["name"] in names:
            if set_at is None or at >= set_at:
                set_at, event_id = at, event.get("id")
        elif kind == "commented":
            if commented is None or at > commented:
                commented = at
    if set_at is None:
        return None
    return {"set": set_at, "event_id": event_id, "commented": commented}


def judge(info, now, days):
    """None while the label is within its limit or a comment came after it (the issue stays quiet); `EXPIRED` once it is older than `days` with no comment
    since; `UNKNOWN` when the age is not known (info is None)."""
    if info is None:
        return UNKNOWN
    if now - info["set"] <= days * 86400 or (info["commented"] is not None and info["commented"] > info["set"]):
        return None
    return EXPIRED
