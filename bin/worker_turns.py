"""How a worker's turns are read (#1102): a tool-only turn shows its tool names and a short scrubbed summary, a failed turn its error, and one
summary line tells a working worker from a stalled one. Called by `message_line` and `cmd_read` of bin/cloud_worker.py, for local and cloud workers.

    turn_text(message, scrub)       -> the text of a turn: its text, else one line per tool part, else the error, else ""
    summary_line(messages, scrub)   -> `turns=N tools=N last_tool=NAME last_error=NAME/STATUS last_activity=TIME`  (messages in any order)

`scrub(text, known, limit)` is the launcher's one scrubber. Every printed piece is scrubbed and cut to SUMMARY_MAX characters. Standard library only.
"""
import json

SUMMARY_MAX = 80   # the bound of one tool's argument summary and of an error's text


def _clean(value, scrub, limit=SUMMARY_MAX):
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    flat = " ".join(text.split())
    return scrub(scrub(flat, (), 2000), (), limit)[:limit]


def _parts(message):
    parts = message.get("parts", message.get("content"))
    return [p for p in parts if isinstance(p, dict)] if isinstance(parts, list) else []


def tool_parts(message):
    return [p for p in _parts(message) if p.get("type") in ("tool", "tool_use", "tool-call", "tool_call")]


def _tool_name(part):
    return str(part.get("tool") or part.get("name") or "?")[:40]


def error_of(message):
    """(name, status) of a failed turn, or None."""
    err = message.get("error")
    if not err:
        return None
    if not isinstance(err, dict):
        return str(err)[:40], ""
    data = err.get("data") if isinstance(err.get("data"), dict) else {}
    status = err.get("statusCode", data.get("statusCode", err.get("status", data.get("status", ""))))
    return str(err.get("name") or "error")[:40], str(status)[:10]


def _tool_line(part, scrub):
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    args = state.get("input", part.get("input", part.get("arguments")))
    line = "tool %s" % _tool_name(part)
    return line + (" %s" % _clean(args, scrub) if args else "")


def turn_text(message, scrub):
    text = message.get("text")
    if not isinstance(text, str):
        text = "\n".join(p["text"] for p in _parts(message) if p.get("type") in (None, "text") and isinstance(p.get("text"), str))
    if text.strip():
        return text
    lines = [_tool_line(p, scrub) for p in tool_parts(message)]
    failed = error_of(message)
    if failed:
        lines.append(("error %s %s" % (_clean(failed[0], scrub, 40), _clean(failed[1], scrub, 10))).rstrip())
    return "\n".join(lines)


def summary_line(messages, scrub):
    turns = [m for m in messages if isinstance(m, dict) and (m.get("type") or m.get("role")) == "assistant"]
    turns.sort(key=lambda m: str(((m.get("time") or {}).get("created") if isinstance(m.get("time"), dict) else m.get("time")) or ""))
    tools = [p for m in turns for p in tool_parts(m)]
    errors = [e for e in (error_of(m) for m in turns) if e]
    stamp = ""
    for m in turns:
        moment = m.get("time")
        stamp = str((moment.get("completed") or moment.get("created")) if isinstance(moment, dict) else moment or stamp)
    return "turns=%d tools=%d last_tool=%s last_error=%s last_activity=%s" % (
        len(turns), len(tools), _clean(_tool_name(tools[-1]), scrub, 40) if tools else "none",
        _clean("%s/%s" % errors[-1], scrub, 50) if errors else "none", _clean(stamp, scrub, 30) or "unknown")
