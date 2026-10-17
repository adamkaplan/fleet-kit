"""What a plain `fleet-switchboard launch` starts (#433, the default switch): a worker with no terminal ("headless") or one in a herdr tab ("terminal").

The module declares its own configuration key (bin/fleet-switchboard config_declarations). The shipped default is "headless"; setting the key to "terminal"
in the configuration restores a tab with no code change. Only `launch` with neither `--headless` nor `--terminal` (and no `--workspace` or
`--new-workspace`) reads it. Python 3.9 standard library only.
"""

CONFIG_KEY = "launch_default"   # the module declares its own configuration key
CONFIG_DEFAULT = "headless"     # the shipped default: no terminal unless asked for
MODES = ("headless", "terminal")


def validate(raw, source="config"):
    """`launch_default` must be "headless" or "terminal". Raises ValueError with a message naming the key."""
    if raw not in MODES:
        raise ValueError("%s: 'launch_default' must be \"headless\" or \"terminal\", got %r" % (source, raw))


def mode(config):
    """The configured mode for a plain launch. An unknown value cannot get here (`validate` refuses the whole configuration); if one did, it reads as the shipped default."""
    value = (config or {}).get(CONFIG_KEY)
    return value if value in MODES else CONFIG_DEFAULT
