"""Configuration declaration for `caretaker_other_owners` (#2573).

Declares `caretaker_other_owners` for `bin/fleet-switchboard`'s `config_declarations()` so the
Tuesday/Thursday Caretaker's list of other-owner GitHub logins is validated and never triggers
an unknown-key warning.
"""

CONFIG_KEY = "caretaker_other_owners"
CONFIG_DEFAULT = None
MAX_ENTRIES = 100


def validate(raw, source):
    """Raises ValueError naming the key: absent or null, or a list of non-empty login strings."""
    if raw is None:
        return
    if not isinstance(raw, list) or len(raw) > MAX_ENTRIES or not all(isinstance(x, str) and x.strip() for x in raw):
        raise ValueError(
            "%s: '%s' must be a list of at most %d non-empty GitHub login strings, got %r"
            % (source, CONFIG_KEY, MAX_ENTRIES, raw)
        )
