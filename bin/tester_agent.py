"""#1963: `launch --role tester` needs an agent whose definition says it can be launched as a tester.

The allowed list is READ from the definitions in agents/opencode/ of the switchboard's own checkout (one front matter line, `fleet-roles: coder, tester`):
never typed here and never read from ~/.config. The agent's name is the definition's front matter `name:`, or its file name when it has none."""
import os

PROG = "fleet-switchboard"


def line_for(agent, root):
    """None when `agent` may be launched with --role tester, else the one line to print (no newline). `root` is the checkout (the directory above bin/).
    Fails closed: an unreadable directory, or one with no tester definition, refuses."""
    folder = os.path.join(root, "agents", "opencode")
    allowed = []
    try:
        for entry in sorted(os.listdir(folder)):
            if not entry.endswith(".md"):
                continue
            with open(os.path.join(folder, entry), encoding="utf-8") as handle:
                text = handle.read()
            front = text.split("\n---", 2)[0].splitlines()[1:] if text.startswith("---\n") else []
            fields = {}
            for line in front:
                key, _, value = line.partition(":")
                if line and not line.startswith((" ", "#")):
                    fields[key.strip()] = value.strip()
            if "tester" in [r.strip() for r in fields.get("fleet-roles", "").split(",")]:
                allowed.append(fields.get("name") or entry[:-3])
    except (OSError, UnicodeDecodeError) as err:
        return ("%s: launch: --role tester: cannot read the agent definitions in %s (%s); run fleet-switchboard from the switchboard's checkout; nothing was started"
                % (PROG, folder, getattr(err, "strerror", None) or err))
    if not allowed:
        return ("%s: launch: --role tester: no definition in %s declares `fleet-roles: ... tester`; run fleet-switchboard from the switchboard's checkout "
                "(or add the line to the definition); nothing was started" % (PROG, folder))
    if agent in allowed:
        return None
    return ("%s: launch: --role tester needs an agent that works a brief and records verdicts: %s is not one (allowed: %s); "
            "run: fleet-switchboard launch NAME --agent %s --role tester ...; nothing was started" % (PROG, agent, ", ".join(allowed), allowed[0]))
