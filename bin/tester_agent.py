"""#1963: `launch --role tester` needs an agent whose line in agents/roles.tsv names the role `tester`.

The allowed list is READ from agents/roles.tsv of the switchboard's own checkout (one line for each agent: its name, a TAB, its roles separated by commas; a line that
begins with # is a comment): never typed here and never read from ~/.config. No definition is edited."""
import os

PROG = "fleet-switchboard"


def line_for(agent, root):
    """None when `agent` may be launched with --role tester, else the one line to print (no newline). `root` is the checkout (the directory above bin/).
    Fails closed: an unreadable file, or one with no line naming tester, refuses."""
    path = os.path.join(root, "agents", "roles.tsv")
    allowed = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle.read().splitlines():
                if not line.strip() or line.startswith("#"):
                    continue
                name, _, roles = line.partition("\t")
                if name.strip() and "tester" in [r.strip() for r in roles.split(",")]:
                    allowed.append(name.strip())
    except (OSError, UnicodeDecodeError) as err:
        return ("%s: launch: --role tester: cannot read %s (%s); run fleet-switchboard from the switchboard's checkout; nothing was started"
                % (PROG, path, getattr(err, "strerror", None) or err))
    if not allowed:
        return ("%s: launch: --role tester: no line of %s names tester; run fleet-switchboard from the switchboard's checkout "
                "(or add the line to the file); nothing was started" % (PROG, path))
    if agent in allowed:
        return None
    return ("%s: launch: --role tester needs an agent that works a brief and records verdicts: %s is not one (allowed: %s); "
            "run: fleet-switchboard launch NAME --agent %s --role tester ...; nothing was started" % (PROG, agent, ", ".join(allowed), allowed[0]))
