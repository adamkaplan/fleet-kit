V1_EXPORT_TIMEOUT = 120      # seconds `opencode export` may take


V1_ID_RE = re.compile(r"^ses_[A-Za-z0-9]+$")


V1_LIST_MAX_PAGES = 500      # beyond this the search is incomplete, and we refuse


V1_FINISH = ("stop", "length", "tool-calls", "content-filter", "error", "unknown")


V1_TOOL_NAMES = {"bash": "shell"}  # v1's tool names that v2 spells differently (measured: v2's is `shell`)


V1_AUDIT_EXAMPLES = 50       # dropped items named one by one in the audit; the rest are counted


class ImportV1Error(Exception):
    """An import that cannot go ahead, and why. Nothing was written to v2."""


V2Client.session_import = _v2_session_import


V2Client.session_list_page = _v2_session_list_page


# ------------------------------------------------------- the shapes we emit
#
# The subset of v2's schemas (Session.Info, Session.Message.User and
# .Assistant, their parts) that the converter writes, as JSON-schema-like
# dicts, and a checker small enough to read. v2 refuses a body that does not
# match; checking first means a bad conversion never reaches it.

def _shape(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


_S_STR, _S_NUM = {"type": "string"}, {"type": "number"}


_S_BOOL, _S_ANY_OBJECT = {"type": "boolean"}, {"type": "object"}


_S_BASE64 = {"type": "string", "pattern": r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$"}


_S_MENTION = _shape({"start": _S_NUM, "end": _S_NUM, "text": _S_STR}, ("start", "end", "text"))


_S_MODEL = _shape({"id": _S_STR, "providerID": _S_STR, "variant": _S_STR}, ("id", "providerID"))


_S_TOKENS = _shape({"input": _S_NUM, "output": _S_NUM, "reasoning": _S_NUM,
                    "cache": _shape({"read": _S_NUM, "write": _S_NUM}, ("read", "write"))},
                   ("input", "output", "reasoning", "cache"))


_S_ERROR = _shape({"type": _S_STR, "message": _S_STR, "status": {"type": "integer", "minimum": 100},
                   "response": _shape({"body": _S_STR}, ("body",))}, ("type", "message"))


_S_TOOL_CONTENT = {"anyOf": [
    _shape({"type": {"enum": ["text"]}, "text": _S_STR}, ("type", "text")),
    _shape({"type": {"enum": ["file"]}, "uri": _S_STR, "mime": _S_STR,
            "name": {"anyOf": [_S_STR, {"type": "null"}]}}, ("type", "uri", "mime"))]}


_S_TOOL_STATE = {"anyOf": [
    _shape({"status": {"enum": ["streaming"]}, "input": _S_STR}, ("status", "input")),
    _shape({"status": {"enum": ["running"]}, "input": _S_ANY_OBJECT, "metadata": _S_ANY_OBJECT},
           ("status", "input", "metadata")),
    _shape({"status": {"enum": ["completed"]}, "input": _S_ANY_OBJECT, "content": {"type": "array", "items": _S_TOOL_CONTENT},
            "metadata": _S_ANY_OBJECT}, ("status", "input", "content")),
    _shape({"status": {"enum": ["error"]}, "input": _S_ANY_OBJECT, "error": _S_ERROR,
            "content": {"type": "array", "items": _S_TOOL_CONTENT}, "metadata": _S_ANY_OBJECT},
           ("status", "input", "error"))]}


_S_CONTENT = {"anyOf": [
    _shape({"type": {"enum": ["text"]}, "text": _S_STR}, ("type", "text")),
    _shape({"type": {"enum": ["reasoning"]}, "text": _S_STR,
            "time": _shape({"created": _S_NUM, "completed": _S_NUM}, ("created",))}, ("type", "text")),
    _shape({"type": {"enum": ["tool"]}, "id": _S_STR, "name": _S_STR, "executed": _S_BOOL, "state": _S_TOOL_STATE,
            "time": _shape({"created": _S_NUM, "ran": _S_NUM, "completed": _S_NUM}, ("created",))},
           ("type", "id", "name", "state", "time"))]}


_S_MSG_ID = {"type": "string", "pattern": r"^msg_"}


_S_USER = _shape({
    "id": _S_MSG_ID, "metadata": _S_ANY_OBJECT, "time": _shape({"created": _S_NUM}, ("created",)),
    "text": _S_STR, "type": {"enum": ["user"]},
    "files": {"type": "array", "items": _shape({
        "data": _S_BASE64, "mime": _S_STR, "name": _S_STR, "description": _S_STR, "mention": _S_MENTION,
        "source": {"anyOf": [_shape({"type": {"enum": ["inline"]}}, ("type",)),
                             _shape({"type": {"enum": ["uri"]}, "uri": _S_STR}, ("type", "uri"))]}},
        ("data", "mime", "source"))},
    "agents": {"type": "array", "items": _shape({"name": _S_STR, "mention": _S_MENTION}, ("name",))},
}, ("id", "time", "text", "type"))


_S_ASSISTANT = _shape({
    "id": _S_MSG_ID, "metadata": _S_ANY_OBJECT, "type": {"enum": ["assistant"]},
    "time": _shape({"created": _S_NUM, "streamed": _S_NUM, "completed": _S_NUM}, ("created",)),
    "agent": _S_STR, "model": _S_MODEL, "content": {"type": "array", "items": _S_CONTENT},
    "snapshot": _shape({"start": _S_STR, "end": _S_STR, "files": {"type": "array", "items": _S_STR}}),
    "finish": {"enum": list(V1_FINISH)}, "rawFinish": _S_STR, "cost": _S_NUM, "tokens": _S_TOKENS,
    "error": _S_ERROR,
    "retry": _shape({"attempt": {"type": "integer"}, "at": _S_NUM, "error": _S_ERROR}, ("attempt", "at", "error")),
}, ("id", "time", "type", "agent", "model", "content"))


V2_IMPORT_INFO_SCHEMA = _shape({
    "id": {"type": "string", "pattern": r"^ses"}, "parentID": {"type": "string", "pattern": r"^ses"},
    "projectID": _S_STR, "agent": _S_STR, "model": _S_MODEL, "cost": _S_NUM, "tokens": _S_TOKENS,
    "time": _shape({"created": _S_NUM, "updated": _S_NUM, "idle": _S_NUM, "viewed": _S_NUM, "archived": _S_NUM},
                   ("created", "updated")),
    "title": _S_STR, "metadata": _S_ANY_OBJECT, "location": _shape({"directory": _S_STR}, ("directory",)),
}, ("id", "projectID", "cost", "tokens", "time", "location"))


V2_IMPORT_MESSAGE_SCHEMA = {"anyOf": [_S_USER, _S_ASSISTANT]}


V2_IMPORT_SCHEMA = _shape({
    "info": V2_IMPORT_INFO_SCHEMA, "messages": {"type": "array", "items": V2_IMPORT_MESSAGE_SCHEMA},
    "location": _shape({"directory": _S_STR}, ("directory",)),
}, ("info", "messages"))


_SCHEMA_TYPES = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
                 "string": lambda v: isinstance(v, str), "boolean": lambda v: isinstance(v, bool),
                 "number": lambda v: _is_number(v) and v == v and v not in (float("inf"), float("-inf")),
                 "integer": lambda v: isinstance(v, int) and not isinstance(v, bool), "null": lambda v: v is None}


def _discriminator(branch):
    """(field, value) when a branch of a union pins one field to one value."""
    for field, rule in (branch.get("properties") or {}).items():
        if isinstance(rule, dict) and len(rule.get("enum", ())) == 1:
            yield field, rule["enum"][0]


def _union_errors(value, schema, path):
    """Errors of a union: of the one branch the value's own tag selects, else of none matching."""
    branches, failures = schema["anyOf"], []
    for branch in branches:
        errors = schema_errors(value, branch, path)
        if not errors:
            return []
        if isinstance(value, dict) and any(value.get(f) == v for f, v in _discriminator(branch)):
            return errors
        failures.append(errors[0])
    return ["%s: matches none of the %d allowed shapes (%s)" % (path, len(branches), "; ".join(failures[:2]))]


def schema_errors(value, schema, path="$"):
    """What is wrong with `value` against `schema` (the subset used above); [] when nothing."""
    if "anyOf" in schema:
        return _union_errors(value, schema, path)
    if "enum" in schema:
        return [] if value in schema["enum"] else ["%s: %r is not one of %s" % (path, value, schema["enum"])]
    expected = schema.get("type")
    if expected and not _SCHEMA_TYPES[expected](value):
        return ["%s: expected %s, got %s" % (path, expected, _one_line(repr(value), 40))]
    errors = []
    if "pattern" in schema and not re.search(schema["pattern"], value):
        errors.append("%s: %s does not match %s" % (path, _one_line(repr(value), 40), schema["pattern"]))
    if "minimum" in schema and value < schema["minimum"]:
        errors.append("%s: %r is below %r" % (path, value, schema["minimum"]))
    if expected == "array":
        for index, item in enumerate(value):
            errors.extend(schema_errors(item, schema["items"], "%s[%d]" % (path, index)))
    if expected == "object":
        errors.extend(_object_errors(value, schema, path))
    return errors


def _object_errors(value, schema, path):
    errors = ["%s.%s: required" % (path, name) for name in schema.get("required", ()) if name not in value]
    properties = schema.get("properties")
    for name, item in sorted(value.items()):
        if properties is None:
            continue
        if name not in properties:
            errors.append("%s.%s: not a field v2 accepts here" % (path, name))
        else:
            errors.extend(schema_errors(item, properties[name], "%s.%s" % (path, name)))
    return errors


def v2_import_problems(info, messages, location=None):
    """Everything v2 would refuse in this body, found before anything is sent."""
    body = {"info": info, "messages": messages}
    if location is not None:
        body["location"] = location
    try:
        json.dumps(body, allow_nan=False)
    except (TypeError, ValueError) as err:
        return ["body is not plain JSON: %s" % err]
    problems = schema_errors(body, V2_IMPORT_SCHEMA)
    ids = [m.get("id") for m in messages if isinstance(m, dict)]
    problems.extend("messages: id %s is used twice" % i for i in sorted({i for i in ids if ids.count(i) > 1}))
    return problems


# ------------------------------------------------------------- the converter
#
# v1's `opencode export` prints {info, messages: [{info, parts: [...]}]}. v2
# has no parts: a user message carries its text and attachments, an assistant
# message carries an ordered `content` list. One v1 message becomes exactly one
# v2 message, so the count and the order survive. What v2 has no place for is
# never lost quietly: it goes into `dropped`, which the command prints and
# audits; what v2 requires and v1 did not say is filled in and listed in
# `defaulted`.
import base64


from urllib.parse import unquote_to_bytes


def v1_target_id(prefix, v1_session, v1_id="", position=0):
    """A v2 id from a v1 one, the same on every run (so a retry is the same import).

    26 hex digits like the ids this tool already makes. A message id starts with
    its position in the transcript, so sorting by id keeps v1's order whichever
    way v2 lists them.
    """
    digest = hashlib.sha256(json.dumps(["v1-import", prefix, v1_session, v1_id],
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    return prefix + "_" + (format(position, "012x") + digest[:14] if prefix == "msg" else digest[:26])


def _need(condition, what):
    if not condition:
        raise ImportV1Error("not a usable v1 export: " + what)


def _created_ms(time, what):
    created = time.get("created") if isinstance(time, dict) else None
    _need(_is_number(created) and created >= 0, "%s has no numeric time.created" % what)
    return created


class V1Tally:
    """What the conversion dropped or had to fill in."""

    def __init__(self):
        self.dropped, self.defaulted = [], []

    def drop(self, message, part, kind, reason):
        part_id = part.get("id") if isinstance(part, dict) else None
        self.dropped.append({"message": message, "part": part_id, "kind": kind, "reason": reason})

    def default(self, message, field, value, reason):
        self.defaulted.append({"message": message, "field": field, "value": value, "reason": reason})


def _mention_of(box):
    """v1's {value, start, end} as v2's mention, or None."""
    if isinstance(box, dict) and isinstance(box.get("value"), str) and _is_number(box.get("start")) \
            and _is_number(box.get("end")):
        return {"start": box["start"], "end": box["end"], "text": box["value"]}
    return None


def _mention(source):
    """The mention of a v1 file part's `source` (its `text` holds the span)."""
    return _mention_of(source.get("text") if isinstance(source, dict) else None)


def _data_url(url):
    """The base64 data of a `data:` URL, else None."""
    match = re.match(r"^data:[^;,]*((?:;[^;,]*)*),(.*)$", url, re.DOTALL)
    if not match:
        return None
    if ";base64" in match.group(1):
        return match.group(2)
    return base64.b64encode(unquote_to_bytes(match.group(2))).decode("ascii")


def _file_attachment(part, message, tally):
    url, mime = part.get("url"), part.get("mime")
    if not isinstance(url, str) or not isinstance(mime, str):
        tally.drop(message, part, "file", "a file part without a url and a mime type")
        return None
    attachment = {"mime": mime}
    inline = _data_url(url)
    if inline is not None:
        attachment["data"], attachment["source"] = inline, {"type": "inline"}
    else:
        attachment["data"], attachment["source"] = "", {"type": "uri", "uri": url}
        tally.default(message, "file.data", "", "contents of %s are not embedded: the reference is kept" % url)
    if isinstance(part.get("filename"), str):
        attachment["name"] = part["filename"]
    mention = _mention(part.get("source"))
    if mention:
        attachment["mention"] = mention
    elif part.get("source"):
        tally.drop(message, part, "file-source", "v1's file source (path, range) has no v2 equivalent")
    return attachment


def _agent_attachment(part):
    attachment = {"name": part["name"]}
    mention = _mention_of(part.get("source"))
    if mention:
        attachment["mention"] = mention
    return attachment


def _is_typed_text(part):
    return part.get("type") == "text" and isinstance(part.get("text"), str) \
        and not part.get("synthetic") and not part.get("ignored")


def _user_part(part, message, tally, sink):
    """Sort one part of a v1 user message into `sink` (texts, files, agents) or the dropped list."""
    kind = part.get("type") if isinstance(part, dict) else None
    if not isinstance(part, dict):
        tally.drop(message, None, "unreadable", "a part that is not an object")
    elif _is_typed_text(part):
        sink["texts"].append(part["text"])
    elif kind == "text" and part.get("synthetic"):
        tally.drop(message, part, "synthetic-text",
                   "text v1 injected, not typed; a v2 user message holds only what was typed")
    elif kind == "text":
        tally.drop(message, part, "ignored-text", "text v1 kept out of the model's context")
    elif kind == "file":
        attachment = _file_attachment(part, message, tally)
        if attachment:
            sink["files"].append(attachment)
    elif kind == "agent" and isinstance(part.get("name"), str):
        sink["agents"].append(_agent_attachment(part))
    else:
        tally.drop(message, part, str(kind), "no place for this part in a v2 user message")


def convert_v1_user(message, tally):
    info = message["info"]
    sink = {"texts": [], "files": [], "agents": []}
    for part in message["parts"]:
        _user_part(part, info["id"], tally, sink)
    out = {"type": "user", "time": {"created": _created_ms(info.get("time"), "message " + info["id"])},
           "text": "\n\n".join(sink["texts"])}
    for field in ("files", "agents"):
        if sink[field]:
            out[field] = sink[field]
    return out


# ---- assistant messages

def _structured_error(error):
    """v1's {name, data: {message}} (or a bare string) as v2's {type, message}."""
    if isinstance(error, dict):
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        name = error.get("name") if isinstance(error.get("name"), str) else "error"
        return {"type": name, "message": data["message"] if isinstance(data.get("message"), str) else name}
    return {"type": "error", "message": str(error)}


def _tool_contents(state, limit, message, tally):
    content = []
    output = state.get("output")
    if isinstance(output, str) and output:
        if limit is not None and len(output) > limit:
            tally.default(message, "tool.output", output[:limit],
                          "tool output cut from %d to %d characters (--tool-output-limit)" % (len(output), limit))
            output = output[:limit]
        content.append({"type": "text", "text": output})
    for attachment in state.get("attachments") or ():
        if isinstance(attachment, dict) and isinstance(attachment.get("url"), str):
            item = {"type": "file", "uri": attachment["url"], "mime": attachment.get("mime") or "application/octet-stream"}
            if isinstance(attachment.get("filename"), str):
                item["name"] = attachment["filename"]
            content.append(item)
    return content


def _tool_state(state, limit, message, tally):
    """v1's pending/running/completed/error as v2's streaming/running/completed/error."""
    status, given = state.get("status"), state.get("input")
    if not isinstance(given, dict):
        given = {}
    metadata = dict(state["metadata"]) if isinstance(state.get("metadata"), dict) else {}
    if isinstance(state.get("title"), str) and "title" not in metadata:
        metadata["title"] = state["title"]
    if status == "pending":
        return {"status": "streaming", "input": state["raw"] if isinstance(state.get("raw"), str) else ""}
    if status == "running":
        return {"status": "running", "input": given, "metadata": metadata}
    if status == "completed":
        out = {"status": "completed", "input": given, "content": _tool_contents(state, limit, message, tally)}
    elif status == "error":
        out = {"status": "error", "input": given, "error": _structured_error(state.get("error", "error"))}
    else:
        return None
    if metadata:
        out["metadata"] = metadata
    return out


def _tool_part(part, created, limit, message, tally):
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    converted = _tool_state(state, limit, message, tally)
    if converted is None or not isinstance(part.get("tool"), str) or not isinstance(part.get("callID"), str):
        tally.drop(message, part, "tool", "a tool call v1 recorded without a name, id or known state")
        return None
    span = state.get("time") if isinstance(state.get("time"), dict) else {}
    time = {"created": span["start"] if _is_number(span.get("start")) else created}
    if _is_number(span.get("start")):
        time["ran"] = span["start"]
    if _is_number(span.get("end")):
        time["completed"] = span["end"]
    if part.get("metadata"):
        tally.drop(message, part, "tool-metadata", "provider metadata on a tool call has no v2 equivalent")
    return {"type": "tool", "id": part["callID"], "name": V1_TOOL_NAMES.get(part["tool"], part["tool"]),
            "state": converted, "time": time}


def _reasoning_part(part, created):
    out = {"type": "reasoning", "text": part["text"]}
    span = part.get("time") if isinstance(part.get("time"), dict) else {}
    if _is_number(span.get("start")):
        out["time"] = {"created": span["start"]}
        if _is_number(span.get("end")):
            out["time"]["completed"] = span["end"]
    return out


class V1Assistant:
    """Collects one assistant message: its content, and the snapshot its markers imply."""

    def __init__(self, message, limit, tally):
        self.info, self.parts, self.limit, self.tally = message["info"], message["parts"], limit, tally
        self.content, self.snapshot, self.retry = [], {}, None
        self.created = _created_ms(self.info.get("time"), "message " + self.info["id"])

    def run(self):
        for part in self.parts:
            self.take(part)
        return self.result()

    def take(self, part):
        kind = part.get("type") if isinstance(part, dict) else None
        handler = getattr(self, "part_" + re.sub(r"\W", "_", str(kind)), None)
        if not isinstance(part, dict):
            self.tally.drop(self.info["id"], None, "unreadable", "a part that is not an object")
        elif handler is None:
            self.tally.drop(self.info["id"], part, "unknown:%s" % kind, "a part kind this converter does not know")
        else:
            handler(part)

    def part_text(self, part):
        if not isinstance(part.get("text"), str):
            self.tally.drop(self.info["id"], part, "text", "a text part without text")
        elif part.get("synthetic") or part.get("ignored"):
            self.tally.drop(self.info["id"], part, "synthetic-text" if part.get("synthetic") else "ignored-text",
                            "text kept out of the model's context, or injected, in v1")
        else:
            self.content.append({"type": "text", "text": part["text"]})

    def part_reasoning(self, part):
        if isinstance(part.get("text"), str):
            self.content.append(_reasoning_part(part, self.created))
        else:
            self.tally.drop(self.info["id"], part, "reasoning", "a reasoning part without text")

    def part_tool(self, part):
        item = _tool_part(part, self.created, self.limit, self.info["id"], self.tally)
        if item:
            self.content.append(item)

    def part_step_start(self, part):
        if isinstance(part.get("snapshot"), str) and "start" not in self.snapshot:
            self.snapshot["start"] = part["snapshot"]
        self.tally.drop(self.info["id"], part, "step-marker", "v2 has no step markers (a snapshot hash, if any, is kept)")

    def part_step_finish(self, part):
        if isinstance(part.get("snapshot"), str):
            self.snapshot["end"] = part["snapshot"]
        self.tally.drop(self.info["id"], part, "step-marker", "v2 has no step markers (a snapshot hash, if any, is kept)")

    def part_patch(self, part):
        files = [f for f in part.get("files") or () if isinstance(f, str)]
        self.snapshot["files"] = sorted(set(self.snapshot.get("files", [])) | set(files))

    def part_retry(self, part):
        span = part.get("time") if isinstance(part.get("time"), dict) else {}
        if _is_number(part.get("attempt")) and _is_number(span.get("created")):
            self.retry = {"attempt": int(part["attempt"]), "at": span["created"], "error": _structured_error(part.get("error"))}
        else:
            self.tally.drop(self.info["id"], part, "retry", "a retry record without an attempt and a time")

    def part_snapshot(self, part):
        self.tally.drop(self.info["id"], part, "snapshot", "a bare snapshot part has no v2 equivalent")

    def part_file(self, part):
        self.tally.drop(self.info["id"], part, "file", "a file part inside an assistant message has no v2 equivalent")

    def part_agent(self, part):
        self.tally.drop(self.info["id"], part, "agent", "an agent mention has no v2 equivalent in an assistant message")

    def part_subtask(self, part):
        self.tally.drop(self.info["id"], part, "subtask", "a subtask request has no v2 equivalent here; the child session is its own import")

    def part_compaction(self, part):
        self.tally.drop(self.info["id"], part, "compaction", "a compaction marker has no v2 equivalent that keeps one message per message")

    def agent_and_model(self):
        info, mid = self.info, self.info["id"]
        agent = info.get("agent") if isinstance(info.get("agent"), str) else info.get("mode")
        if not isinstance(agent, str) or not agent:
            agent = "build"
            self.tally.default(mid, "agent", agent, "v1 recorded no agent for this message")
        model = {"id": info.get("modelID"), "providerID": info.get("providerID")}
        if not (isinstance(model["id"], str) and isinstance(model["providerID"], str)):
            model = {"id": "unknown", "providerID": "unknown"}
            self.tally.default(mid, "model", model, "v1 recorded no model for this message")
        return agent, model

    def result(self):
        info = self.info
        agent, model = self.agent_and_model()
        out = {"type": "assistant", "time": {"created": self.created}, "agent": agent, "model": model,
               "content": self.content}
        if _is_number((info.get("time") or {}).get("completed")):
            out["time"]["completed"] = info["time"]["completed"]
        if self.snapshot:
            out["snapshot"] = self.snapshot
        if isinstance(info.get("finish"), str):
            out["finish"] = info["finish"] if info["finish"] in V1_FINISH else "unknown"
            if out["finish"] != info["finish"]:
                out["rawFinish"] = info["finish"]
        if _is_number(info.get("cost")):
            out["cost"] = info["cost"]
        tokens = v1_tokens(info.get("tokens"))
        if tokens:
            out["tokens"] = tokens
        if info.get("error"):
            out["error"] = _structured_error(info["error"])
        if self.retry:
            out["retry"] = self.retry
        return out


def v1_tokens(raw):
    """v1's token counts as v2's, or None when v1 did not record them all."""
    cache = raw.get("cache") if isinstance(raw, dict) and isinstance(raw.get("cache"), dict) else None
    fields = [raw.get(n) for n in ("input", "output", "reasoning")] if cache is not None else []
    if cache is None or not all(_is_number(v) for v in fields + [cache.get("read"), cache.get("write")]):
        return None
    return {"input": fields[0], "output": fields[1], "reasoning": fields[2],
            "cache": {"read": cache["read"], "write": cache["write"]}}


def convert_v1_assistant(message, limit, tally):
    return V1Assistant(message, limit, tally).run()


# ---- the whole session

def _checked_v1_export(export, directory=None):
    _need(isinstance(export, dict) and isinstance(export.get("info"), dict)
          and isinstance(export.get("messages"), list), "expected an object with `info` and `messages`")
    info = export["info"]
    _need(isinstance(info.get("id"), str) and V1_ID_RE.match(info["id"]), "info.id must look like ses_...")
    _need(directory is not None or (isinstance(info.get("directory"), str) and os.path.isabs(info["directory"])),
          "info.directory must be an absolute path (v2 needs a location); or pass --dir")
    for index, message in enumerate(export["messages"]):
        where = "messages[%d]" % index
        _need(isinstance(message, dict) and isinstance(message.get("info"), dict)
              and isinstance(message.get("parts"), list), "%s needs `info` and `parts`" % where)
        _need(isinstance(message["info"].get("id"), str) and message["info"]["id"], "%s.info.id is missing" % where)
        _need(message["info"].get("role") in ("user", "assistant"),
              "%s has role %r; only user and assistant convert" % (where, message["info"].get("role")))
    return info


def _session_totals(messages):
    cost, tokens = 0, {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
    for message in messages:
        cost += message.get("cost", 0)
        used = message.get("tokens")
        if used:
            for name in ("input", "output", "reasoning"):
                tokens[name] += used[name]
            tokens["cache"]["read"] += used["cache"]["read"]
            tokens["cache"]["write"] += used["cache"]["write"]
    return cost, tokens


def _v2_session_info(v1, messages, v2_id, fleet, directory=None):
    cost, tokens = _session_totals(messages)
    stamps = [m["time"]["created"] for m in messages]
    time = v1.get("time") if isinstance(v1.get("time"), dict) else {}
    created = _created_ms(time, "info")
    info = {"id": v2_id, "projectID": v1["projectID"] if isinstance(v1.get("projectID"), str) else "global",
            "cost": cost, "tokens": tokens, "location": {"directory": directory or v1["directory"]},
            "time": {"created": created,
                     "updated": max([time["updated"] if _is_number(time.get("updated")) else created] + stamps)}}
    if isinstance(v1.get("title"), str):
        info["title"] = v1["title"]
    if isinstance(v1.get("parentID"), str) and V1_ID_RE.match(v1["parentID"]):
        info["parentID"] = v1_target_id("ses", v1["parentID"])
    assistants = [m for m in messages if m["type"] == "assistant"]
    if assistants:
        info["agent"], info["model"] = assistants[-1]["agent"], assistants[-1]["model"]
    if fleet is not None and not fault("no-import-metadata"):
        info["metadata"] = {"fleet": dict(fleet, imported_from="v1:" + v1["id"])}
    return info


def convert_v1_export(export, fleet=None, tool_output_limit=None, directory=None):
    """v1's export as {info, messages, dropped, defaulted, v1_session, v2_session}.

    Pure: nothing is read or written. `fleet` is the agent's metadata.fleet
    (name, role, ...); the converter adds `imported_from`. Raises ImportV1Error
    for an export it cannot trust to keep the count (an unknown role, no ids).
    """
    v1 = _checked_v1_export(export, directory)
    tally, messages = V1Tally(), []
    for position, message in enumerate(export["messages"]):
        converted = (convert_v1_user(message, tally) if message["info"]["role"] == "user"
                     else convert_v1_assistant(message, tool_output_limit, tally))
        converted["id"] = v1_target_id("msg", v1["id"], message["info"]["id"], position)
        messages.append(converted)
    v2_id = v1_target_id("ses", v1["id"])
    return {"v1_session": v1["id"], "v2_session": v2_id, "messages": messages, "dropped": tally.dropped,
            "defaulted": tally.defaulted, "info": _v2_session_info(v1, messages, v2_id, fleet, directory)}


def import_order(exports):
    """The exports with every parent before its children (v2 refuses a child first).

    A parent that is not among the exports is assumed to exist already.
    """
    ids = {e["info"]["id"] for e in exports}
    ordered, placed = [], set()
    pending = list(exports)
    while pending:
        ready = [e for e in pending if e["info"].get("parentID") not in ids or e["info"]["parentID"] in placed]
        if not ready:
            raise ImportV1Error("the sessions' parents form a cycle: " + ", ".join(e["info"]["id"] for e in pending))
        for export in ready:
            ordered.append(export)
            placed.add(export["info"]["id"])
            pending.remove(export)
    return ordered


def summarise_dropped(dropped):
    """[{kind, reason, count}] from the dropped list, most numerous first."""
    counts = {}
    for item in dropped:
        counts[(item["kind"], item["reason"])] = counts.get((item["kind"], item["reason"]), 0) + 1
    rows = [{"kind": kind, "reason": reason, "count": count} for (kind, reason), count in counts.items()]
    return sorted(rows, key=lambda row: (-row["count"], row["kind"]))


# ------------------------------------------------------------- the command

def read_v1_export(args, config, runner):
    """The v1 export JSON from `--file`, or from the configured v1 binary's `export`."""
    if args.file:
        try:
            text = Path(args.file).read_text()
        except OSError as err:
            raise ImportV1Error("cannot read %s: %s" % (args.file, err.strerror or err)) from None
    else:
        text = _run_v1_export(args.session, config, runner)
    try:
        return json.loads(text)
    except ValueError as err:
        raise ImportV1Error("the v1 export is not valid JSON: %s" % err) from None


def _run_v1_export(session, config, runner):
    binary = config["v1_opencode"]
    if binary is None:
        raise ImportV1Error("'v1_opencode' is not configured in %s: set it to the absolute path of the "
                            "OpenCode v1 binary (it is only ever asked to `export`), or use --file"
                            % config_path())
    if not V1_ID_RE.match(session or ""):
        raise ImportV1Error("--session must be a v1 session id such as ses_..., got %r" % session)
    try:
        result = runner([binary, "export", session], capture_output=True, text=True,
                        timeout=V1_EXPORT_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ImportV1Error("%s export did not finish within %ds" % (binary, V1_EXPORT_TIMEOUT)) from None
    except OSError as err:
        raise ImportV1Error("cannot run %s: %s" % (binary, err.strerror or err)) from None
    if result.returncode != 0:
        raise ImportV1Error("%s export %s failed: %s" % (binary, session, _one_line(result.stderr or result.stdout)))
    return result.stdout or ""


def find_imported(v2, v1_id, directory):
    """The v2 session already imported from this v1 session, or None.

    Found by listing (`metadata.fleet.imported_from`), page by page. If the
    listing cannot be read to its end, that is an error: an unproven absence
    would let a second copy in.
    """
    wanted, cursor = "v1:" + v1_id, None
    for _ in range(V1_LIST_MAX_PAGES):
        sessions, cursor = v2.session_list_page(directory, cursor)
        for session in sessions:
            if (_fleet_meta(session) or {}).get("imported_from") == wanted:
                return session
        if cursor is None:
            return None
    raise ImportV1Error("v2 has more than %d sessions in %s; cannot prove %s was not imported already"
                        % (V1_LIST_MAX_PAGES * V1_LIST_PAGE, directory, wanted))


def _body_size(conversion):
    body = {"info": conversion["info"], "messages": conversion["messages"],
            "location": conversion["info"]["location"]}
    return len(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _pure_refusals(conversion, limit):
    """Reasons not to send that need no call: v2 would refuse the body, or it is too long."""
    problems = v2_import_problems(conversion["info"], conversion["messages"], conversion["info"]["location"])
    if problems:
        return ["the converted session breaks v2's schema (%d problem(s)); nothing was sent: %s"
                % (len(problems), "; ".join(problems[:5]))]
    size = _body_size(conversion)
    if size > limit:
        return ["the body is %d bytes and `opencode api` takes it inline; the limit is %d "
                "(config import_body_limit_bytes). Try --tool-output-limit N to cut long tool output, "
                "which is listed as defaulted" % (size, limit)]
    return []


def _missing_parent(v2, conversion):
    """A reason not to send when the session's parent is not in v2 yet, else []."""
    parent = conversion["info"].get("parentID")
    if not parent:
        return []
    try:
        v2.session_get(parent)
    except V2Error as err:
        if err.status != 404:
            raise
        return ["its parent %s is not in v2 yet; import the parent first (v2 refuses a child before "
                "its parent)" % parent]
    return []


def _audit_detail(conversion, fleet):
    shown = conversion["dropped"][:V1_AUDIT_EXAMPLES]
    return {"v1_session": conversion["v1_session"], "v2_session": conversion["v2_session"],
            "messages": len(conversion["messages"]), "fleet": fleet,
            "dropped_total": len(conversion["dropped"]), "dropped_by_kind": summarise_dropped(conversion["dropped"]),
            "dropped_examples": shown, "dropped_unlisted": len(conversion["dropped"]) - len(shown),
            "defaulted": conversion["defaulted"][:V1_AUDIT_EXAMPLES],
            "defaulted_total": len(conversion["defaulted"])}


def import_v1_session(v2, audit, export, fleet, limit, tool_output_limit=None, dry_run=False, directory=None):
    """Convert, check, and (unless dry-run) import one v1 session. Returns a report dict.

    Nothing is sent when the conversion is refused. A session already imported
    is reported and left alone. `audit` is None only for a dry run.
    """
    conversion = convert_v1_export(export, fleet, tool_output_limit, directory)
    report = {"status": "dry-run" if dry_run else "imported", "v1_session": conversion["v1_session"],
              "v2_session": conversion["v2_session"], "messages": len(conversion["messages"]),
              "dropped": conversion["dropped"], "defaulted": conversion["defaulted"],
              "body_bytes": _body_size(conversion)}
    refusals = _pure_refusals(conversion, limit)  # before any call, even a read
    if refusals:
        raise ImportV1Error("; ".join(refusals))
    existing = find_imported(v2, conversion["v1_session"], conversion["info"]["location"]["directory"])
    if existing:
        return dict(report, status="exists", v2_session=existing["id"])
    refusals = _missing_parent(v2, conversion)
    if refusals:
        raise ImportV1Error("; ".join(refusals))
    if dry_run:
        return report
    try:
        with audit.external("cli", "v2.session.import", fleet.get("name") if fleet else None,
                            _audit_detail(conversion, fleet)) as result:
            result["session"] = v2.session_import(conversion["info"], conversion["messages"],
                                                  conversion["info"]["location"])["id"]
    except V2Error as err:
        if err.status == 409:  # the second layer: v2 already holds this (deterministic) session id
            return dict(report, status="exists")
        raise
    return report


def format_import_report(report):
    verb = {"imported": "imported", "exists": "already imported (nothing done)",
            "dry-run": "would import"}[report["status"]]
    lines = ["%s: v1 %s -> v2 %s, %d message(s)" % (verb, report["v1_session"], report["v2_session"], report["messages"])]
    if report["status"] == "exists":
        return "\n".join(lines)
    lines.append("  body %d bytes" % report["body_bytes"])
    rows = summarise_dropped(report["dropped"])
    lines.append("  dropped %d item(s)%s" % (len(report["dropped"]), ":" if rows else ""))
    lines.extend("    %5d  %-16s %s" % (row["count"], row["kind"], row["reason"]) for row in rows)
    lines.append("  filled in %d value(s)%s" % (len(report["defaulted"]), ":" if report["defaulted"] else ""))
    lines.extend("    %s %s: %s" % (item["message"], item["field"], item["reason"]) for item in report["defaulted"][:20])
    return "\n".join(lines)


def _import_fleet(args):
    problems = []
    if not NAME_RE.match(args.name or ""):
        problems.append("--name must match %s, got %r" % (NAME_RE.pattern, args.name))
    for flag, value in (("--issue", args.issue), ("--charter", args.charter)):
        if value is not None and not _is_issue_number(value):
            problems.append("%s must be an issue number, got %r" % (flag, value))
    if args.dir is not None and not os.path.isabs(args.dir):
        problems.append("--dir must be an absolute path, got %r" % args.dir)
    if args.tool_output_limit is not None and args.tool_output_limit < 1:
        problems.append("--tool-output-limit must be at least 1, got %r" % args.tool_output_limit)
    if problems:
        raise ImportV1Error("; ".join(problems))
    fleet = fleet_record(args.name, args.role, args.reports_to, args.issue)
    if args.charter is not None:
        fleet["charter"] = args.charter
    return fleet


def cmd_import_v1(args, runner, now):
    config = load_config()
    active_faults()
    if config["opencode"] is None:
        raise ConfigError("'opencode' is not configured in %s: set it to the absolute path of the "
                          "OpenCode v2 binary" % config_path())
    try:
        fleet = _import_fleet(args)
        export = read_v1_export(args, config, runner)
        v2 = V2Client(config["opencode"], config["opencode_env"], runner, config["call_timeout_seconds"], transport=config["v2_transport"])
        audit = None if args.dry_run else AuditLog(ensure_state_dir() / "audit.jsonl")
        report = import_v1_session(v2, audit, export, fleet, config["import_body_limit_bytes"],
                                   args.tool_output_limit, args.dry_run, args.dir)
    except ImportV1Error as err:
        sys.stderr.write("%s: %s\n" % (PROG, _one_line(str(err), 600)))
        return EXIT_ERROR
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_import_report(report))
    return EXIT_OK


V1_MOVE_COMMANDS = {"import-v1": cmd_import_v1}


def add_v1_move_parsers(sub):
    move = sub.add_parser("import-v1", help="Copy a v1 session into v2 as a fleet agent's session; v1 is only read")
    source = move.add_mutually_exclusive_group(required=True)
    source.add_argument("--session", help="The v1 session id (read with the configured v1 binary's `export`)")
    source.add_argument("--file", help="A file holding what v1's `opencode export` printed")
    move.add_argument("--name", required=True, help="The agent's fleet name (lower case, digits, - and _)")
    move.add_argument("--role", help="metadata.fleet.role")
    move.add_argument("--reports-to", dest="reports_to", help="metadata.fleet.reports_to")
    move.add_argument("--issue", type=int, help="metadata.fleet.issue")
    move.add_argument("--charter", type=int, help="metadata.fleet.charter (an orchestrator's charter issue)")
    move.add_argument("--tool-output-limit", dest="tool_output_limit", type=int,
                      help="Cut each tool output to this many characters (listed as filled in)")
    move.add_argument("--dir", help="The session's working directory in v2 (absolute); default: the one v1 recorded")
    move.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="Convert and check; print what would be imported; write nothing")
    move.add_argument("--json", action="store_true", help="Print the report as JSON")
