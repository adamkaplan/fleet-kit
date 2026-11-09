"""A brief given to `launch --resume` is DELIVERED or the command says it was not (ask #512).

`launch NAME --resume SESSION --brief-file PATH` used to accept the option and drop it: the resumed agent answered the last thing it had seen.
Now the brief is read BEFORE any pane opens (an empty, unreadable or oversize brief is refused with exit 2 and nothing opened), and after the
pane is proven to run that session it is sent once with the same delivery a fresh launch uses (`send_brief`: a prompt marked as the brief,
audited with its length and never its text; never typed into the pane). The output says so: `"brief": "sent"` in the result line and a line
on standard error. A send that fails leaves the pane open and says so: exit 1, "the pane is open without the brief", with the reason.
A resume with no brief is not touched by any of this. The brief is read from the given file or standard input and nowhere else.

The logic is here; `bin/fleet-switchboard` calls it from the resume branch of `launch` with a namespace `sb` of its own helpers (`NEEDS`).
"""

import json

BRIEF_MAX_CHARS = 16000   # a brief longer than this is refused before the pane opens (a fresh launch has no limit; a resume says so loudly)

NEEDS = ("PROG", "EXIT_OK", "EXIT_ERROR", "LaunchError", "send_brief", "_one_line")


class BriefRefused(Exception):
    """The brief cannot be used, and why. Nothing was opened or sent."""


def read(sb_read_brief, path, stdin):
    """The brief text from the given file or standard input (`-`), or BriefRefused: unreadable, empty, or over BRIEF_MAX_CHARS."""
    try:
        text = sb_read_brief(path, stdin)
    except Exception as err:   # LaunchError from the shared reader: cannot read it, or it is empty
        raise BriefRefused(str(err)) from None
    if len(text) > BRIEF_MAX_CHARS:
        raise BriefRefused("the brief %s is %d characters; a brief given to a resume is at most %d: say less, link more" % (path, len(text), BRIEF_MAX_CHARS))
    return text


def deliver(sb, engine, done, brief, out, err):
    """Send `brief` to the resumed agent (its pane is proven to run the session: `resume_agent` returned) and say so. Returns the exit code.
    `done` is the resume's result dict; it gains `brief`: "sent" or "not sent"."""
    name, session = done["name"], done["session"]
    try:
        sb.send_brief(engine, name, session, brief)
    except Exception as problem:   # any failure: the brief did not arrive, and the pane is open without it
        done["brief"] = "not sent"
        out(json.dumps(done, sort_keys=True))
        err("%s: the brief was NOT delivered to %s (%s): the pane is open without the brief; send it with `%s send %s ...`"
            % (sb.PROG, name, sb._one_line(str(problem), 160), sb.PROG, name))
        return sb.EXIT_ERROR
    done["brief"] = "sent"
    out(json.dumps(done, sort_keys=True))
    err("%s: the brief was delivered to %s (%d characters)" % (sb.PROG, name, len(brief)))
    return sb.EXIT_OK
