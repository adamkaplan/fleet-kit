"""external_adapter: a generic client for an outside system that holds decisions (and work items) for the Switchboard.

It implements the ExternalDecisionAdapter port of bin/decision_core.py and is driven entirely by a LOCAL MAPPING
FILE: this module knows no product, endpoint, field or enum name. Everything specific to the system on the other
side (its URL, its paths, its tool names, its field names, its enum words, which header carries the credential)
lives in the mapping, which is a JSON file on the owner's machine and is never committed. Only generic, protocol
level words are fixed here (HTTP, JSON-RPC, the MCP streamable-HTTP method names, the generic vocabulary of the
decision design: tiers, dispositions, event types).

What the other side is assumed to offer (a contract of capabilities, nothing more):
  - two ways in with one credential: a request/response HTTP API and a tool-calling protocol (MCP over streamable
    HTTP, JSON-RPC over POST). Decisions may go over either (the mapping's `channel` is the primary, the other one
    is the fallback for an operation the primary does not define); work reporting is HTTP only;
  - no push: answers arrive by polling ONE monotonic cursor feed; a read with the last cursor returns only newer
    changes and the same cursor when nothing happened, so a repeated read is safe;
  - raise is idempotent by an id we send (same id and body: the same item; same id, different body: a conflict);
    revise, withdraw and "acted" exist; a work item has its own id and an idempotent create;
  - an answer carries a disposition (accept, edit, reject, clarify), the chosen OPTION ID (we act on the id, never
    the words), and who gave it. clarify is not an answer: the item is sent back.

The mapping file (JSON; unknown top-level keys are refused, an inline credential is refused):
  mapping_version  1
  base_url         http(s)://host[:port]; cleartext is accepted only for a loopback host
  tool_path        path of the tool channel;  http_prefix  prefix of every HTTP operation path
  auth             {header, scheme}: the credential goes in `<header>: <scheme> <key>`
  key_source       see below; a list of up to two sources is a rotation (the next one is tried on a refusal)
  channel          "tool" or "http": the primary channel for decisions;  project: the project/seat id used
  timeouts         {request, command} seconds;  retry {attempts, base_delay, max_delay, max_retry_after_sleep,
                   default_retry_after};  poll_interval (s);  work_interval (s, at least 60: updates are coalesced)
  idempotency      {namespace: a UUID}: our key -> a stable name-based UUID (a key that already is a UUID is kept)
  mcp              {session_header, protocol_version, client_name}
  operations       per operation (feed, raise, items, revise, withdraw, acted, status, work_put, work_patch,
                   work_finish, work_get, work_list): {tool, http: {method, path}, id_arg, cursor_param}; `{id}`
                   and `{project}` in a path are filled in. feed, raise and items are required
  fields           field names of every request: raise, option, evidence, revise, withdraw, acted, work
  responses        field names of every response: item_id, item (the read shape), feed, change, answer, work, page
                   (page {info, next}: a reply that says it is truncated names the token of the next page; an
                   operation's `next_param` is the request field that carries it; every page is read)
  enums            generic -> the other side's words: tier, disposition, status, change_kind, outcome,
                   work_state, work_end, integration_status, meaning (disposition -> allow|deny|text|needs_info)
  tier_map         the fleet's tier (orchestrator, cos, human) -> {class, floor}: the FLOOR consequence tier we
                   state; the other side may rate higher, never lower
  roles            [{when: {field: value...}, role: orchestrator|cos|human}]: who answered -> the authority it has
  errors           {code_field, message_field, status_field, retry_after_field, codes: {code: ErrorClassName}}
  options          {id_prefix}: ids we send with options (index based) when the decision's own have none
  defaults         {recommendation}: used when a decision has none (the other side requires one)

Key sources (the key is never in the mapping, never logged, never in an error text; scrubbed from any text; held in
memory only):
  env:NAME            an environment variable
  file:PATH           a file that must be mode 0600 (no group or other bits) or it is refused
  command:[argv]      (or {"command": [argv], "timeout": s}) run once by the daemon at start with a timeout and no
                      shell; stdout is the key, stderr is discarded: this is how a vault fetch works

What it exposes: McpRestAdapter (the port: capabilities, health, push for raise/revise/withdraw/status,
pull_changes -> (events, next_cursor), lookup, acted, read_items) and McpRestAdapter.work (a WorkClient: put,
patch with coalescing, finish, get, list). `build_adapter(mapping_path)` makes one from a mapping file;
bin/fleet-switchboard loads this file by path, as it loads decision_core.

Normalised events (pull_normalised): {event_id, external_id, type, seq, at, ...} with type one of
answered (answer: {id, disposition, meaning, option_id, text, rationale, clarification, rejection}, by: {role,
raw}), sent_back (a clarify: no answer), rated (tier, floor), revised (text, awaits_rating), reversed (answer,
replaces, by), withdrawn, other.

Port version: the step-4 contract of bin/decision_core.py (branch switchboard/decision-sync): push(operation,
idempotency_key) with operation {op: raise|revise|status|withdraw, key, external_id, revision, decision, floor,
note} returns {external_id, url}; lookup(key) -> {external_id, revision} | None; pull_changes(cursor) -> (events,
next_cursor) with port events {event_id, external_id, type: answered|clarify|reversed|withdrawn, answered_by:
project agent|owner's agent|human (unknown when no role rule matches), option_id, text, at}. acted(), read_items(),
pull_normalised() and the work client are extras outside the port. A status operation (a tier change or a note)
when the mapping defines none is a no-op that stays local, not an error: the engine would retry an error forever.

Python 3.9+, standard library only.
"""

import collections
import hashlib
import http.client
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# ---------------------------------------------------------------- loading the port


def _load_core():
    """decision_core, loaded by path from beside this file (the way bin/fleet-switchboard loads it); a copy already
    loaded from the same file is reused so the port class is one class."""
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "decision_core.py")
    held = sys.modules.get("decision_core")
    if held is not None and os.path.realpath(getattr(held, "__file__", "") or "") == path:
        return held
    spec = importlib.util.spec_from_file_location("decision_core", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["decision_core"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop("decision_core", None)
        raise
    return module


decision_core = _load_core()



# ---------------------------------------------------------------- errors

TIERS = ("orchestrator", "cos", "human")
CONSEQUENCES = ("low", "moderate", "high", "reserved")
DISPOSITIONS = ("accept", "edit", "reject", "clarify")
DEFAULT_MEANING = {"accept": "allow", "edit": "text", "reject": "deny", "clarify": "needs_info"}


class AdapterError(Exception):
    """Base of everything this module raises on purpose. Texts never carry a key.

    `permanent` tells the sync engine whether trying the same operation again can help: True for a refusal that will
    not change by itself (a rejected request, an unknown item, an already decided one, a refused credential, a
    disabled integration); False for what may pass (RateLimited, Unavailable)."""

    permanent = False

    def __init__(self, message, status=None, code=None):
        Exception.__init__(self, message)
        self.status, self.code = status, code


class MappingError(AdapterError):
    permanent = True


class KeySourceError(AdapterError):
    permanent = True


class CredentialError(AdapterError):
    """No credential, a refused one or a revoked one (401)."""

    permanent = True


class DisabledError(AdapterError):
    """The integration is disabled on the other side (403 or 409 as the mapping says)."""

    permanent = True


class NotFound(AdapterError):
    permanent = True


class InvalidRequest(AdapterError):
    """A 400, a 415, a schema failure on the tool channel, or a request we refused to build."""

    permanent = True


class Conflict(AdapterError):
    permanent = True


class AlreadyDecided(Conflict):
    """The item is already decided or finished: read its answer instead."""


class RateLimited(AdapterError):
    def __init__(self, message, retry_after, status=429, code=None):
        AdapterError.__init__(self, message, status, code)
        self.retry_after = retry_after


class Unavailable(AdapterError):
    """5xx, a timeout, a refused connection: the other side cannot be used now."""


class Unsupported(AdapterError):
    """The mapping defines no such operation."""

    permanent = True


ERROR_CLASSES = {c.__name__: c for c in (CredentialError, DisabledError, NotFound, InvalidRequest, Conflict,
                                         AlreadyDecided, RateLimited, Unavailable)}


# ---------------------------------------------------------------- keys


class Secret:
    """A key held in memory. It never prints, never pickles and has no text form."""
    __slots__ = ("_value",)

    def __init__(self, value):
        object.__setattr__(self, "_value", value)

    def reveal(self):
        return self._value

    def __repr__(self):
        return "<key>"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("a key is not stored")

    def __eq__(self, other):
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self):
        return hash("secret")


def scrub(text, secrets=()):
    """`text` with every held key and anything shaped like a credential header value taken out."""
    text = str(text if text is not None else "")
    for secret in secrets:
        value = secret.reveal() if isinstance(secret, Secret) else secret
        if value:
            text = text.replace(value, "[key]")
            text = text.replace(urllib.parse.quote(value, safe=""), "[key]")
    return re.sub(r"(?i)\b(bearer|basic|token)\s+[^\s\"',;]+", r"\1 [key]", text)


def parse_key_source(spec):
    """A key source spec -> ("env", name) | ("file", path) | ("command", argv, timeout|None). Raises MappingError."""
    if isinstance(spec, dict):
        argv, timeout = spec.get("command"), spec.get("timeout")
        if (set(spec) - {"command", "timeout"} or not isinstance(argv, list) or not argv
                or not all(isinstance(a, str) and a for a in argv)
                or (timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                             or timeout <= 0))):
            raise MappingError("key_source: a command source is {\"command\": [argv...], \"timeout\": seconds}")
        return ("command", argv, timeout)
    if not isinstance(spec, str):
        raise MappingError("key_source: expected env:NAME, file:PATH or command:[argv]")
    kind, _, rest = spec.partition(":")
    if kind == "env" and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", rest):
        return ("env", rest)
    if kind == "file" and rest:
        return ("file", rest)
    if kind == "command":
        try:
            argv = json.loads(rest)
        except ValueError:
            raise MappingError("key_source: command: takes a JSON list of arguments")
        return parse_key_source({"command": argv})
    raise MappingError("key_source: expected env:NAME, file:PATH or command:[argv] (a key is never inline)")


def load_key(source, command_timeout=10, environ=None, run=subprocess.run):
    """Read one key from a parsed source. Raises KeySourceError (its text never holds the key)."""
    environ = os.environ if environ is None else environ
    if source[0] == "env":
        value = environ.get(source[1], "")
        if not value.strip():
            raise KeySourceError("key source: environment variable %s is not set" % source[1])
        return Secret(value.strip())
    if source[0] == "file":
        path = os.path.expanduser(source[1])
        try:
            info = os.stat(path)
        except OSError as error:
            raise KeySourceError("key source: cannot stat %s (%s)" % (path, error.strerror or "error"))
        if not stat.S_ISREG(info.st_mode):
            raise KeySourceError("key source: %s is not a regular file" % path)
        if info.st_mode & 0o077:
            raise KeySourceError("key source: %s must be mode 0600 (it has group or other access): refused" % path)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = handle.read().strip()
        except (OSError, UnicodeDecodeError):
            raise KeySourceError("key source: cannot read %s" % path)
        if not value:
            raise KeySourceError("key source: %s is empty" % path)
        return Secret(value)
    argv, timeout = source[1], source[2] or command_timeout
    try:
        done = run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                   timeout=timeout, shell=False, check=False)
    except subprocess.TimeoutExpired:
        raise KeySourceError("key source: the command did not answer in %s s" % timeout)
    except (OSError, ValueError) as error:
        raise KeySourceError("key source: the command could not run (%s)" % type(error).__name__)
    if done.returncode != 0:
        raise KeySourceError("key source: the command failed (exit %s)" % done.returncode)
    value = (done.stdout or b"").decode("utf-8", "replace").strip()
    if not value:
        raise KeySourceError("key source: the command printed no key")
    return Secret(value)


class KeyRing:
    """The keys of up to two sources (a rotation). Loaded once; `rotate` loads the next (or the same again)."""

    def __init__(self, sources, command_timeout=10, environ=None, run=subprocess.run):
        self.sources, self.command_timeout, self.environ, self.run = sources, command_timeout, environ, run
        self.index = 0
        self.secret = None

    def current(self):
        if self.secret is None:
            self.secret = load_key(self.sources[self.index], self.command_timeout, self.environ, self.run)
        return self.secret

    def rotate(self):
        """Move to the next source (or reload the only one); True when the key is a different one now."""
        old = self.secret
        self.index = (self.index + 1) % len(self.sources)
        self.secret = None
        try:
            self.current()
        except KeySourceError:
            self.secret = old
            return False
        return old is None or self.secret != old

    def secrets(self):
        return [self.secret] if self.secret is not None else []


# ---------------------------------------------------------------- the mapping file

TOP_KEYS = {"mapping_version", "base_url", "tool_path", "http_prefix", "auth", "key_source", "channel", "project",
            "timeouts", "retry", "poll_interval", "work_interval", "idempotency", "mcp", "operations", "fields",
            "responses", "enums", "tier_map", "roles", "errors", "options", "defaults"}
FORBIDDEN_KEYS = {"key", "secret", "token", "password", "api_key", "apikey", "credential", "bearer"}
DECISION_OPS = ("feed", "raise", "items", "revise", "withdraw", "acted", "status")
WORK_OPS = ("work_put", "work_patch", "work_finish", "work_get", "work_list")
WORK_METHODS = {"work_put": "PUT", "work_patch": "PATCH", "work_finish": "POST", "work_get": "GET",
                "work_list": "GET"}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
ID_OPS = ("revise", "withdraw", "acted", "status")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
RETRY_DEFAULTS = {"attempts": 3, "base_delay": 0.5, "max_delay": 8.0, "max_retry_after_sleep": 5.0,
                  "default_retry_after": 1.0}
MIN_WORK_INTERVAL = 60


class Mapping:
    """A validated mapping. `data` is the file's content; the helpers below are the only readers of it."""

    def __init__(self, data):
        self.data = data
        self.retry = dict(RETRY_DEFAULTS, **data.get("retry", {}))
        self.timeouts = dict({"request": 10, "command": 10}, **data.get("timeouts", {}))
        self.operations = data["operations"]
        self.fields = data["fields"]
        self.responses = data["responses"]
        self.enums = data["enums"]
        self.errors = data.get("errors", {})
        self.channel = data["channel"]

    def to_external(self, group, generic):
        return self.enums[group][generic]

    def to_generic(self, group, external):
        for generic, value in self.enums.get(group, {}).items():
            if value == external:
                return generic
        return None

    def op(self, name):
        found = self.operations.get(name)
        if not found:
            raise Unsupported("the mapping defines no %s operation" % name)
        return found


class MappingFile:
    """Loads and validates the local mapping file."""

    @staticmethod
    def load(path):
        try:
            with open(os.path.expanduser(str(path)), "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except OSError as error:
            raise MappingError("mapping file cannot be read: %s" % (error.strerror or "error"))
        except ValueError as error:
            raise MappingError("mapping file is not JSON: %s" % error)
        return MappingFile.from_dict(data)

    @staticmethod
    def from_dict(data):
        problems = MappingFile.problems(data)
        if problems:
            raise MappingError("mapping invalid: " + "; ".join(problems[:20]) + ("; ..." if len(problems) > 20 else ""))
        return Mapping(data)

    @staticmethod
    def problems(data):
        found = []
        if not isinstance(data, dict):
            return ["the mapping must be a JSON object"]

        def need(cond, text):
            if not cond:
                found.append(text)
            return cond

        unknown = sorted(set(data) - TOP_KEYS)
        need(not unknown, "unknown top-level keys: %s" % ", ".join(unknown))
        for key in sorted(data):
            if isinstance(key, str) and key.lower() in FORBIDDEN_KEYS:
                found.append("%s: a credential is never inline (use key_source)" % key)
        need(data.get("mapping_version") == 1, "mapping_version must be 1")

        base = data.get("base_url")
        if need(isinstance(base, str) and base, "base_url is required"):
            parts = urllib.parse.urlsplit(base)
            if need(parts.scheme in ("http", "https") and parts.hostname, "base_url must be http(s)://host"):
                need(not parts.username and not parts.password, "base_url must not carry a credential")
                need(not parts.query and not parts.fragment, "base_url must have no query or fragment")
                need(parts.scheme == "https" or parts.hostname in LOOPBACK,
                     "base_url: cleartext http is accepted only for a loopback host")
        for key in ("tool_path", "http_prefix"):
            value = data.get(key)
            need(isinstance(value, str) and value.startswith("/"), "%s must be a path starting with /" % key)
        auth = data.get("auth")
        if need(isinstance(auth, dict) and isinstance(auth.get("header"), str) and auth.get("header")
                and isinstance(auth.get("scheme", ""), str), "auth needs {header, scheme}"):
            need(re.match(r"^[A-Za-z0-9-]+$", auth["header"]), "auth.header is not a header name")
        sources = data.get("key_source")
        if isinstance(sources, list):
            need(1 <= len(sources) <= 2, "key_source: one source, or two for a rotation")
            listed = sources
        else:
            listed = [sources]
        for one in listed:
            try:
                parse_key_source(one)
            except MappingError as error:
                found.append(str(error))
        need(data.get("channel") in ("tool", "http"), "channel must be tool or http")
        need(isinstance(data.get("project"), str) and data.get("project"), "project is required")
        for key, low in (("poll_interval", 1), ("work_interval", MIN_WORK_INTERVAL)):
            value = data.get(key)
            need(isinstance(value, (int, float)) and not isinstance(value, bool) and value >= low,
                 "%s must be a number >= %s" % (key, low))
        timeouts = data.get("timeouts", {})
        need(isinstance(timeouts, dict) and all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
                                                for v in timeouts.values()), "timeouts must be positive numbers")
        retry = data.get("retry", {})
        if need(isinstance(retry, dict) and set(retry) <= set(RETRY_DEFAULTS), "retry: unknown or malformed"):
            attempts = retry.get("attempts", 3)
            need(isinstance(attempts, int) and not isinstance(attempts, bool) and 1 <= attempts <= 10,
                 "retry.attempts must be 1 to 10")
            for key, value in retry.items():
                if key != "attempts":
                    need(isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0,
                         "retry.%s must be a number >= 0" % key)
        ident = data.get("idempotency", {})
        if need(isinstance(ident, dict) and set(ident) <= {"namespace"}, "idempotency: only {namespace}"):
            try:
                if "namespace" in ident:
                    uuid.UUID(ident["namespace"])
            except (ValueError, AttributeError, TypeError):
                found.append("idempotency.namespace must be a UUID")
        mcp = data.get("mcp", {})
        need(isinstance(mcp, dict) and set(mcp) <= {"session_header", "protocol_version", "client_name"}
             and all(isinstance(v, str) and v for v in mcp.values()), "mcp: {session_header, protocol_version, "
                                                                        "client_name} strings")

        ops = data.get("operations")
        if need(isinstance(ops, dict), "operations is required"):
            for name in sorted(set(ops) - set(DECISION_OPS) - set(WORK_OPS)):
                found.append("operations.%s: unknown operation" % name)
            for name in ("feed", "raise", "items"):
                need(name in ops, "operations.%s is required" % name)
            for name, op in ops.items():
                if name not in DECISION_OPS and name not in WORK_OPS:
                    continue
                if not need(isinstance(op, dict), "operations.%s must be an object" % name):
                    continue
                need(not (set(op) - {"tool", "http", "id_arg", "cursor_param", "next_param"}),
                     "operations.%s: unknown keys" % name)
                need(op.get("next_param") is None or (isinstance(op["next_param"], str) and op["next_param"]),
                     "operations.%s.next_param must be a name" % name)
                tool, http_ = op.get("tool"), op.get("http")
                if name in WORK_OPS:
                    need(tool is None, "operations.%s: work reporting is HTTP only (no tool)" % name)
                    need(isinstance(http_, dict), "operations.%s needs http" % name)
                else:
                    need(tool is not None or http_ is not None, "operations.%s needs tool or http" % name)
                    need(tool is None or (isinstance(tool, str) and tool), "operations.%s.tool must be a name" % name)
                if http_ is not None and need(isinstance(http_, dict), "operations.%s.http malformed" % name):
                    need(http_.get("method") in METHODS, "operations.%s.http.method is not a method" % name)
                    path = http_.get("path")
                    if need(isinstance(path, str) and path.startswith("/"), "operations.%s.http.path" % name):
                        if name in ID_OPS or name in ("work_put", "work_patch", "work_finish", "work_get"):
                            need("{id}" in path, "operations.%s.http.path needs {id}" % name)
                        if name in WORK_METHODS:
                            need(http_.get("method") == WORK_METHODS[name],
                                 "operations.%s.http.method must be %s" % (name, WORK_METHODS[name]))
                if name in ID_OPS and tool is not None:
                    need(isinstance(op.get("id_arg"), str) and op.get("id_arg"), "operations.%s needs id_arg" % name)
                if name == "feed":
                    need(isinstance(op.get("cursor_param"), str) and op.get("cursor_param"),
                         "operations.feed needs cursor_param")

        fields = data.get("fields")
        if need(isinstance(fields, dict), "fields is required"):
            raise_f = fields.get("raise", {})
            for name in ("idempotency", "project", "class", "text", "recommendation", "floor", "options"):
                need(isinstance(raise_f, dict) and isinstance(raise_f.get(name), str),
                     "fields.raise.%s is required" % name)
            need(isinstance(fields.get("option", {}).get("label"), str), "fields.option.label is required")
            if "revise" in (ops or {}):
                rev = fields.get("revise", {})
                need(isinstance(rev.get("idempotency"), str), "fields.revise.idempotency is required")
                need(any(isinstance(rev.get(k), str) for k in ("text", "recommendation", "options")),
                     "fields.revise needs text, recommendation or options")
            if "withdraw" in (ops or {}):
                need(isinstance(fields.get("withdraw", {}).get("reason"), str), "fields.withdraw.reason is required")
            if "acted" in (ops or {}):
                act = fields.get("acted", {})
                for name in ("idempotency", "answer", "outcome"):
                    need(isinstance(act.get(name), str), "fields.acted.%s is required" % name)
            if any(w in (ops or {}) for w in WORK_OPS):
                work = fields.get("work", {})
                need(isinstance(work.get("title"), str), "fields.work.title is required")
                if "work_finish" in ops:
                    need(isinstance(work.get("end"), str) and isinstance(work.get("summary"), str),
                         "fields.work needs end and summary for work_finish")

        resp = data.get("responses")
        if need(isinstance(resp, dict), "responses is required"):
            need(isinstance(resp.get("item_id"), str), "responses.item_id is required")
            feed = resp.get("feed", {})
            need(isinstance(feed.get("cursor"), str) and isinstance(feed.get("changes"), str),
                 "responses.feed needs cursor and changes")
            page = resp.get("page")
            need(page is None or (isinstance(page, dict) and isinstance(page.get("info"), str)
                                  and isinstance(page.get("next"), str)), "responses.page needs info and next")
            change = resp.get("change", {})
            need(isinstance(change.get("kind"), str) and isinstance(change.get("item_id"), str),
                 "responses.change needs kind and item_id")
            answer = resp.get("answer", {})
            for name in ("id", "disposition"):
                need(isinstance(answer.get(name), str), "responses.answer.%s is required" % name)
            item = resp.get("item", {})
            if "items" in (ops or {}):
                need(isinstance(resp.get("items"), str), "responses.items is required")
                need(isinstance(item.get("id"), str) and isinstance(item.get("status"), str),
                     "responses.item needs id and status")

        enums = data.get("enums")
        if need(isinstance(enums, dict), "enums is required"):
            need(isinstance(enums.get("tier"), dict) and set(enums["tier"]) == set(CONSEQUENCES),
                 "enums.tier maps exactly %s" % ", ".join(CONSEQUENCES))
            need(isinstance(enums.get("disposition"), dict) and set(enums["disposition"]) == set(DISPOSITIONS),
                 "enums.disposition maps exactly %s" % ", ".join(DISPOSITIONS))
            kinds = enums.get("change_kind")
            need(isinstance(kinds, dict) and "answered" in kinds, "enums.change_kind needs answered")
            if "acted" in (ops or {}):
                need(isinstance(enums.get("outcome"), dict) and set(enums["outcome"]) == {"done", "undone", "not_undone"},
                     "enums.outcome maps done, undone, not_undone")
            for group in ("work_state", "work_end"):
                if group in enums:
                    need(isinstance(enums[group], dict), "enums.%s must be an object" % group)
            for group, value in enums.items():
                if isinstance(value, dict):
                    need(all(isinstance(v, str) for v in value.values()), "enums.%s values must be strings" % group)
            meaning = enums.get("meaning")
            if meaning is not None:
                need(isinstance(meaning, dict) and set(meaning) == set(DISPOSITIONS),
                     "enums.meaning maps the four dispositions")

        tiers = data.get("tier_map")
        if need(isinstance(tiers, dict) and set(tiers) == set(TIERS), "tier_map maps orchestrator, cos and human"):
            for tier, entry in tiers.items():
                need(isinstance(entry, dict) and isinstance(entry.get("class"), str) and entry.get("class")
                     and entry.get("floor") in CONSEQUENCES,
                     "tier_map.%s needs a class and a floor (%s)" % (tier, ", ".join(CONSEQUENCES)))
        roles = data.get("roles", [])
        if need(isinstance(roles, list), "roles must be a list"):
            for rule in roles:
                need(isinstance(rule, dict) and isinstance(rule.get("when"), dict) and rule.get("when")
                     and rule.get("role") in TIERS, "each role rule needs a non-empty `when` and a role of %s"
                     % ", ".join(TIERS))
        errors = data.get("errors", {})
        if need(isinstance(errors, dict) and set(errors) <= {"code_field", "message_field", "status_field",
                                                               "retry_after_field", "codes"}, "errors: malformed"):
            codes = errors.get("codes", {})
            need(isinstance(codes, dict) and all(v in ERROR_CLASSES for v in codes.values()),
                 "errors.codes values must be one of %s" % ", ".join(sorted(ERROR_CLASSES)))
        opts = data.get("options", {})
        need(isinstance(opts, dict) and set(opts) <= {"id_prefix", "key_marker"}, "options: only {id_prefix, key_marker}")
        defaults = data.get("defaults", {})
        need(isinstance(defaults, dict) and set(defaults) <= {"recommendation"}, "defaults: only {recommendation}")
        return found


# ---------------------------------------------------------------- HTTP

Response = collections.namedtuple("Response", "status headers text")
MAX_BODY = 16 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is never followed: the credential would go with it."""

    def redirect_request(self, *args, **kwargs):
        return None


def _one_line(text, limit=200):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


class RetryPolicy:
    """Never retry a 4xx, except 429 once its Retry-After has passed (when it is short enough to wait here, else the
    caller is told how long). Retry a 5xx, a timeout or a refused connection with a doubling delay and a cap."""

    def __init__(self, settings, sleep=time.sleep):
        self.s, self.sleep = settings, sleep

    def run(self, fn):
        attempts = int(self.s["attempts"])
        for attempt in range(attempts):
            try:
                return fn()
            except RateLimited as error:
                if attempt + 1 >= attempts or error.retry_after > self.s["max_retry_after_sleep"]:
                    raise
                self.sleep(error.retry_after)
            except Unavailable:
                if attempt + 1 >= attempts:
                    raise
                self.sleep(min(self.s["max_delay"], self.s["base_delay"] * (2 ** attempt)))


class Http:
    """One HTTP attempt (urllib, timeouts, no redirects, no proxy) and the status-class error mapping."""

    def __init__(self, mapping, keyring):
        self.m, self.keys = mapping, keyring
        self.base = mapping.data["base_url"].rstrip("/")
        auth = mapping.data["auth"]
        self.header, self.scheme = auth["header"], auth.get("scheme", "")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    def scrub(self, text):
        return scrub(text, self.keys.secrets())

    def fail(self, cls, message, *args, **kwargs):
        return cls(_one_line(self.scrub(message), 300), *args, **kwargs)

    def once(self, method, path, body=None, query=None, headers=None, rotated=False):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode("utf-8")
        key = self.keys.current().reveal()
        send = {"Accept": "application/json", "Content-Type": "application/json",
                self.header: ("%s %s" % (self.scheme, key)).strip()}
        send.update(headers or {})
        if data is None and method == "GET":
            send.pop("Content-Type", None)
        request = urllib.request.Request(url, data=data, method=method, headers=send)
        try:
            with self.opener.open(request, timeout=self.m.timeouts["request"]) as reply:
                text = reply.read(MAX_BODY + 1)
                found = Response(reply.status, {k.lower(): v for k, v in reply.headers.items()},
                                 text.decode("utf-8", "replace"))
                if len(text) > MAX_BODY:
                    raise self.fail(InvalidRequest, "reply too large")
                return found
        except urllib.error.HTTPError as error:
            try:
                text = error.read(MAX_BODY).decode("utf-8", "replace")
            except (OSError, http.client.HTTPException):
                text = ""
            heads = {k.lower(): v for k, v in error.headers.items()} if error.headers else {}
            try:
                raise self.classify(error.code, heads, text)
            except CredentialError:
                if not rotated and self.keys.rotate():
                    return self.once(method, path, body, query, headers, rotated=True)
                raise
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise self.fail(Unavailable, "request failed (%s)" % type(error).__name__)

    def classify(self, status, headers, text, body=None):
        """The exception for an error reply. `body` is the parsed JSON when the caller has it."""
        errors = self.m.errors
        if body is None:
            try:
                body = json.loads(text)
            except ValueError:
                body = None
        code = message = None
        retry_after = None
        if isinstance(body, dict):
            code = body.get(errors.get("code_field", "code"))
            message = body.get(errors.get("message_field", "message"))
            code = code if isinstance(code, str) else None
            if isinstance(message, list):
                message = "; ".join(str(m) for m in message if isinstance(m, str)) or None
            message = message if isinstance(message, str) else None
            if status is None and errors.get("status_field"):
                got = body.get(errors["status_field"])
                status = got if isinstance(got, int) and not isinstance(got, bool) else None
            raw = body.get(errors.get("retry_after_field", "retry_after")) if errors.get("retry_after_field") else None
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                retry_after = float(raw)
        header = (headers or {}).get("retry-after")
        if header is not None:
            try:
                retry_after = float(header)
            except ValueError:
                pass
        named = ERROR_CLASSES.get(errors.get("codes", {}).get(code)) if code else None
        shown = "%s%s" % ("status %s: " % status if status else "", message or code or "refused")
        if named is not None:
            cls = named
        elif status == 401:
            cls = CredentialError
        elif status == 403:
            cls = DisabledError
        elif status == 404:
            cls = NotFound
        elif status == 409:
            cls = Conflict
        elif status == 429:
            cls = RateLimited
        elif status is not None and status >= 500:
            cls = Unavailable
        else:
            cls = InvalidRequest
        if cls is RateLimited:
            wait = retry_after if retry_after is not None else float(self.m.retry["default_retry_after"])
            return self.fail(RateLimited, shown, wait, status or 429, code)
        return self.fail(cls, shown, status, code)


# ---------------------------------------------------------------- MCP (streamable HTTP, JSON-RPC over POST)


def sse_messages(text):
    """The JSON messages of a text/event-stream body."""
    for block in re.split(r"\r?\n\r?\n", text):
        data = [line[5:].lstrip(" ") for line in block.splitlines() if line.startswith("data:")]
        if data:
            try:
                yield json.loads("\n".join(data))
            except ValueError:
                continue


class McpClient:
    """initialize, tools/list, tools/call. A session id the server gives is kept and sent on later calls."""

    def __init__(self, http, mapping):
        self.http, self.m = http, mapping
        cfg = mapping.data.get("mcp", {})
        self.session_header = cfg.get("session_header", "Mcp-Session-Id")
        self.protocol = cfg.get("protocol_version", "2025-03-26")
        self.client_name = cfg.get("client_name", "fleet-switchboard")
        self.path = mapping.data["tool_path"]
        self.session = None
        self.ready = False
        self.next_id = 0

    def reset(self):
        self.session, self.ready = None, False

    def rpc(self, method, params=None, notify=False):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self.next_id += 1
            message["id"] = self.next_id
        headers = {"Accept": "application/json, text/event-stream"}
        if self.session:
            headers[self.session_header] = self.session
        reply = self.http.once("POST", self.path, message, headers=headers)
        sid = reply.headers.get(self.session_header.lower())
        if sid:
            self.session = sid
        if notify:
            return None
        ctype = reply.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            found = [m for m in sse_messages(reply.text) if isinstance(m, dict) and m.get("id") == message["id"]]
            parsed = found[0] if found else None
        else:
            try:
                parsed = json.loads(reply.text)
            except ValueError:
                parsed = None
        if not isinstance(parsed, dict):
            raise self.http.fail(Unavailable, "tool channel: no usable reply to %s" % method)
        if "error" in parsed:
            err = parsed["error"] if isinstance(parsed["error"], dict) else {}
            raise self.http.fail(InvalidRequest, "tool channel error %s: %s" % (err.get("code"), err.get("message")))
        return parsed.get("result")

    def initialize(self):
        self.rpc("initialize", {"protocolVersion": self.protocol, "capabilities": {},
                                "clientInfo": {"name": self.client_name, "version": "1"}})
        self.rpc("notifications/initialized", notify=True)
        self.ready = True

    def ensure(self):
        if not self.ready:
            self.initialize()

    def list_tools(self):
        self.ensure()
        result = self.rpc("tools/list", {})
        return [t.get("name") for t in (result or {}).get("tools", []) if isinstance(t, dict)]

    def call_tool(self, name, arguments):
        """The tool's JSON result (a dict). A result flagged as an error raises the mapped exception."""
        for again in (False, True):
            self.ensure()
            try:
                result = self.rpc("tools/call", {"name": name, "arguments": arguments})
                break
            except (NotFound, InvalidRequest) as error:
                if again or not self.session or error.status not in (400, 404):
                    raise
                self.reset()  # the session is gone on the other side (a restart): start one and ask again, once
        result = result if isinstance(result, dict) else {}
        texts = [c.get("text") for c in result.get("content", []) if isinstance(c, dict) and c.get("type") == "text"]
        text = "\n".join(t for t in texts if isinstance(t, str))
        body = result.get("structuredContent")
        if body is None:
            try:
                body = json.loads(text)
            except ValueError:
                body = None
        if result.get("isError"):
            if body is None or not isinstance(body, dict):
                raise self.http.fail(InvalidRequest, "tool refused: %s" % _one_line(text))
            raise self.http.classify(None, {}, text, body=body)
        if not isinstance(body, dict):
            raise self.http.fail(InvalidRequest, "the tool result is not a JSON object")
        return body


# ---------------------------------------------------------------- the door both adapters share


class Door:
    """Runs one mapped operation over the right channel, with the retry policy around the whole call."""

    def __init__(self, mapping, keyring, sleep=time.sleep):
        self.m = mapping
        self.http = Http(mapping, keyring)
        self.mcp = McpClient(self.http, mapping)
        self.policy = RetryPolicy(mapping.retry, sleep)

    def channel_of(self, name, force=None):
        op = self.m.op(name)
        if force:
            if force not in op:
                raise Unsupported("operation %s has no %s channel" % (name, force))
            return force
        return self.m.channel if self.m.channel in op else ("http" if "http" in op else "tool")

    def call(self, name, body=None, ident=None, query=None, project=None, force=None):
        op = self.m.op(name)
        channel = self.channel_of(name, force)

        def attempt():
            if channel == "tool":
                args = dict(body or {})
                args.update(query or {})
                if ident is not None:
                    args[op["id_arg"]] = ident
                return self.mcp.call_tool(op["tool"], args)
            spec = op["http"]
            path = self.m.data["http_prefix"].rstrip("/") + spec["path"]
            if ident is not None:
                path = path.replace("{id}", urllib.parse.quote(str(ident), safe=""))
            if "{project}" in path:
                path = path.replace("{project}", urllib.parse.quote(str(project or self.m.data["project"]), safe=""))
            reply = self.http.once(spec["method"], path, body if spec["method"] != "GET" else None,
                                   query=query if spec["method"] == "GET" else None)
            if not reply.text.strip():
                return {}
            try:
                parsed = json.loads(reply.text)
            except ValueError:
                raise self.http.fail(InvalidRequest, "the reply is not JSON")
            if not isinstance(parsed, dict):
                raise self.http.fail(InvalidRequest, "the reply is not a JSON object")
            return parsed

        return self.policy.run(attempt)


# ---------------------------------------------------------------- helpers over decisions and answers

class _Changes(tuple):
    """(events, next_cursor), also readable as {"events": ..., "next_cursor": ...}."""

    def __new__(cls, events, next_cursor):
        return tuple.__new__(cls, (events, next_cursor))

    events = property(lambda self: self[0])
    next_cursor = property(lambda self: self[1])

    def __getitem__(self, key):
        if key == "events":
            return tuple.__getitem__(self, 0)
        if key == "next_cursor":
            return tuple.__getitem__(self, 1)
        return tuple.__getitem__(self, key)


Changes = _Changes


ROLE_NAMES = {"orchestrator": "project agent", "cos": "owner's agent", "human": "human"}
PORT_TYPES = {"answered": "answered", "sent_back": "clarify", "reversed": "reversed", "withdrawn": "withdrawn"}


def _text_of(d, key=None, marker=None):
    """The text an outside item carries for a decision: its title, the longer words, who is asking (opaque text),
    batch rows, and a marker line naming the decision key (so the item can be found again by key)."""
    if d.get("text"):
        head = [str(d["text"])]
    else:
        title = str(d.get("title") or d.get("headline") or "").strip()
        body = str(d.get("body") or "").strip()
        if not body and d.get("headline") and str(d["headline"]).strip() != title:
            body = str(d["headline"]).strip()
        head = [p for p in (title, body) if p]
    asker = " ".join(str(x) for x in (d.get("agent"), ("in %s" % d["repo"]) if d.get("repo") else None,
                                      ("on #%s" % d["ask"]) if d.get("ask") else None) if x)
    if asker:
        head.append("From: " + asker)
    rows = d.get("rows")
    if isinstance(rows, dict):
        rows = ["%s. %s" % (k, v) for k, v in rows.items()]
    elif isinstance(rows, list):
        rows = ["%d. %s" % (i + 1, r if isinstance(r, str) else json.dumps(r, sort_keys=True))
                for i, r in enumerate(rows)]
    if rows:
        head.append("\n".join(rows))
    if marker and key:
        head.append("%s %s" % (marker, key))
    return "\n\n".join(head)


def _pick(obj, name):
    """obj[name] for a mapped name that may be None (not mapped)."""
    if not name or not isinstance(obj, dict):
        return None
    return obj.get(name)


class McpRestAdapter(decision_core.ExternalDecisionAdapter):
    """The generic external adapter. See the module docstring."""

    def __init__(self, mapping, keyring=None, sleep=time.sleep, clock=time.monotonic, environ=None):
        self.m = mapping if isinstance(mapping, Mapping) else MappingFile.from_dict(mapping)
        sources = self.m.data["key_source"]
        sources = sources if isinstance(sources, list) else [sources]
        self.keys = keyring or KeyRing([parse_key_source(s) for s in sources], self.m.timeouts["command"], environ)
        self.door = Door(self.m, self.keys, sleep)
        self.work = WorkClient(self.door, self.m, clock) if "work_put" in self.m.operations else None
        self.poll_interval = self.m.data["poll_interval"]
        self.last_cursor = None
        ops = self.m.operations
        caps = {"push", "pull", "options", "idempotent_raise", "tier_routing", "answer_text", "lookup"}
        caps.update(n for n in ("revise", "withdraw", "acted", "status") if n in ops)
        if self.work is not None:
            caps.add("work")
        self.capabilities = frozenset(caps)

    def start(self):
        """Load the key now (a command source runs here), so a bad source is found at start. Returns None or a line."""
        try:
            self.keys.current()
        except KeySourceError as error:
            return str(error)
        return None

    # ------------------------------------------------------------ port

    def health(self):
        try:
            data = self.door.call("feed", query=self._cursor_query(self.last_cursor))
            down = self.m.enums.get("integration_status", {}).get("disabled")
            status = _pick(data, self.m.responses["feed"].get("status"))
            if down is not None and status == down:
                return "the other side reports this integration as disabled"
            return None
        except AdapterError as error:
            return _one_line(self.door.http.scrub(str(error)), 200)
        except Exception as error:  # a health probe never raises
            return _one_line("health check failed (%s)" % type(error).__name__, 200)

    def push(self, operation, idempotency_key):
        """One outbound operation (the port's shape): {op: raise|revise|status|withdraw, key, external_id, revision,
        decision, floor, note}. Returns {"external_id": ..., "url": None}. The decision may also carry the optional
        extras recommendation, options, grounds, stakes, evidence, consequence, body."""
        if not isinstance(operation, dict) or not isinstance(operation.get("decision", {}), dict):
            raise InvalidRequest("push: an operation is a dict with a decision")
        op = operation.get("op")
        d = operation.get("decision") or {}
        key = operation.get("key")
        if op == "raise":
            ident = self._raise(d, idempotency_key, key, operation.get("floor"))
        elif op in ("revise", "status", "withdraw"):
            ident = operation.get("external_id")
            if not ident:
                raise InvalidRequest("%s: no external_id" % op)
            {"revise": self._revise, "status": self._status, "withdraw": self._withdraw}[op](
                ident, d, idempotency_key, key, operation.get("note"))
        else:
            raise InvalidRequest("push: unknown operation %r" % (op,))
        return {"external_id": ident, "url": None}

    def pull_changes(self, cursor):
        """(events, next_cursor) in the port's shape: {event_id, external_id, type: answered|clarify|reversed|
        withdrawn, answered_by: "project agent"|"owner's agent"|"human"|"unknown", option_id, text, at}, plus
        extras (answer_id, disposition, meaning, rationale, rejection, replaces, seq). Ratings, restatements and
        other changes are not port events: they are skipped here (see pull_normalised) and the cursor moves past."""
        events, nxt = self.pull_normalised(cursor)
        return Changes([self._port_event(e) for e in events if e["type"] in PORT_TYPES], nxt)

    def _pages(self, name, query=None):
        """The replies of an operation, following the pages the other side says remain (at most 50)."""
        data = self.door.call(name, query=query)
        pages = [data]
        spec = self.m.responses.get("page")
        param = self.m.operations[name].get("next_param")
        while spec and param and len(pages) < 50:
            info = data.get(spec["info"])
            token = info.get(spec["next"]) if isinstance(info, dict) else None
            if not token:
                break
            data = self.door.call(name, query=dict(query or {}, **{param: token}))
            pages.append(data)
        return pages

    def pull_normalised(self, cursor):
        """Every change normalised (answered, sent_back, reversed, withdrawn, rated, revised, other), with the cursor."""
        pages = self._pages("feed", self._cursor_query(cursor))
        data = pages[-1]
        feed = self.m.responses["feed"]
        raw = []
        for page in pages:
            one = page.get(feed["changes"])
            if one is None:
                one = []
            if not isinstance(one, list):
                raise InvalidRequest("the feed's changes are not a list")
            raw.extend(one)
        events = [e for e in (self._normalise(c) for c in raw if isinstance(c, dict)) if e is not None]
        events.sort(key=lambda e: (e["seq"] is None, e["seq"] if e["seq"] is not None else 0))
        nxt = data.get(feed["cursor"])
        if nxt is None or (isinstance(nxt, int) and isinstance(cursor, int) and nxt < cursor):
            nxt = cursor
        self.last_cursor = nxt
        return Changes(events, nxt)

    def lookup(self, key):
        """{"external_id", "revision"} of the item raised for decision `key`, or None. Found by the marker line the
        raise put in the item's text (mapping options.key_marker); without a marker mapped nothing can be found.
        The revision is reported as 1: a later revise is idempotent, so under-reporting is safe."""
        marker = self.m.data.get("options", {}).get("key_marker")
        if not marker or "items" not in self.m.operations or not self.m.responses["item"].get("text"):
            return None
        wanted = "%s %s" % (marker, key)
        for item in self.read_items():
            lines = str(item.get("text") or "").splitlines()
            if wanted in [line.strip() for line in lines] and not item.get("withdrawn"):
                return {"external_id": item["external_id"], "revision": 1}
        return None

    def _port_event(self, e):
        answer = e.get("answer") or {}
        by = e.get("by") or {}
        text = answer.get("text")
        if e["type"] == "clarify" or e["type"] == "sent_back":
            text = e.get("clarification")
        elif answer.get("disposition") == "reject":
            text = answer.get("rejection") or text
        port = {"event_id": e["event_id"], "external_id": e["external_id"], "type": PORT_TYPES[e["type"]],
                "answered_by": ROLE_NAMES.get(by.get("role"), "unknown"), "option_id": answer.get("option_id"),
                "text": text, "at": e.get("at"), "seq": e.get("seq")}
        if answer:
            port.update(answer_id=answer.get("id"), disposition=answer.get("disposition"),
                        meaning=answer.get("meaning"), rationale=answer.get("rationale"),
                        rejection=answer.get("rejection"))
        if e.get("replaces") is not None:
            port["replaces"] = e["replaces"]
        if e["type"] == "withdrawn":
            port["answered_by"] = ROLE_NAMES.get(by.get("role"), "unknown") if by else "unknown"
            port["text"] = e.get("reason")
        return port

    # ------------------------------------------------------------ beyond the port

    def read_items(self):
        """The items of this integration, normalised: {external_id, status, text, answer, clarification, withdrawn,
        reversals, opened_at, raw}."""
        spec = self.m.responses["item"]
        found = []
        rows = []
        for page in self._pages("items"):
            rows.extend(page.get(self.m.responses["items"]) or [])
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            answer = raw.get(spec.get("answer")) if spec.get("answer") else None
            found.append({"external_id": raw.get(spec["id"]),
                          "status": self.m.to_generic("status", raw.get(spec["status"])) or raw.get(spec["status"]),
                          "text": _pick(raw, spec.get("text")),
                          "answer": self._answer(answer) if isinstance(answer, dict) else None,
                          "clarification": _pick(raw, spec.get("clarification")),
                          "withdrawn": _pick(raw, spec.get("withdrawn")),
                          "reversals": _pick(raw, spec.get("reversals")) or [],
                          "opened_at": _pick(raw, spec.get("opened_at")),
                          "raw": raw})
        return found

    def acted(self, external_id, answer_id, outcome, note=None, idempotency_key=None):
        """Record what the fleet did with an answer (or a reversal) of `external_id`. Replay-safe by the key."""
        self._need("acted")
        f = self.m.fields["acted"]
        if outcome not in self.m.enums["outcome"]:
            raise InvalidRequest("acted: outcome must be done, undone or not_undone")
        body = {f["idempotency"]: self._uuid_of(idempotency_key or "acted:%s:%s:%s" % (external_id, answer_id, outcome)),
                f["answer"]: answer_id, f["outcome"]: self.m.to_external("outcome", outcome)}
        if note and f.get("note"):
            body[f["note"]] = str(note)
        self.door.call("acted", body, ident=external_id)
        return external_id

    # ------------------------------------------------------------ requests

    def _need(self, name):
        if name not in self.m.operations:
            raise Unsupported("the mapping defines no %s operation" % name)

    def _uuid_of(self, key):
        key = str(key)
        try:
            return str(uuid.UUID(key))
        except ValueError:
            pass
        ns = self.m.data.get("idempotency", {}).get("namespace")
        return str(uuid.uuid5(uuid.UUID(ns) if ns else uuid.NAMESPACE_URL, key))

    def _cursor_query(self, cursor):
        if cursor is None or cursor == "":
            return None
        return {self.m.operations["feed"]["cursor_param"]: cursor}

    def _options(self, raw):
        f = self.m.fields["option"]
        prefix = self.m.data.get("options", {}).get("id_prefix")
        made = []
        for index, option in enumerate(raw or []):
            one = option if isinstance(option, dict) else {"label": str(option)}
            label = one.get("label") or one.get("text")
            if not label:
                raise InvalidRequest("an option needs a label")
            row = {f["label"]: str(label)}
            if one.get("cost") is not None and f.get("cost"):
                row[f["cost"]] = one["cost"]
            if one.get("recommended") and f.get("recommended"):
                row[f["recommended"]] = True
            ident = one.get("id") or (None if prefix is None else "%s%d" % (prefix, index + 1))
            if ident is not None and f.get("id"):
                row[f["id"]] = str(ident)
            made.append(row)
        return made

    def _floor_of(self, d, floor=None):
        entry = self.m.data["tier_map"].get(d.get("tier"))
        if entry is None:
            raise InvalidRequest("no tier_map entry for tier %r" % (d.get("tier"),))
        # the operation's floor (the engine's mapping of the tier) when it is a generic consequence name, else ours
        floor = floor if floor in CONSEQUENCES else entry["floor"]
        hint = d.get("consequence")
        if hint in CONSEQUENCES and CONSEQUENCES.index(hint) > CONSEQUENCES.index(floor):
            floor = hint
        return entry["class"], floor

    def _raise(self, d, key, decision_key=None, floor=None):
        f = self.m.fields["raise"]
        text = _text_of(d, decision_key, self.m.data.get("options", {}).get("key_marker"))
        recommendation = d.get("recommendation") or self.m.data.get("defaults", {}).get("recommendation")
        if not text:
            raise InvalidRequest("raise: the decision has no text")
        if not recommendation:
            raise InvalidRequest("raise: a recommendation is required and none was given")
        label, floor = self._floor_of(d, floor)
        body = {f["idempotency"]: self._uuid_of(key), f["project"]: self.m.data["project"], f["class"]: label,
                f["text"]: text, f["recommendation"]: str(recommendation),
                f["floor"]: self.m.to_external("tier", floor), f["options"]: self._options(d.get("options"))}
        for name in ("default", "grounds", "stakes"):
            if d.get(name) and f.get(name):
                body[f[name]] = d[name]
        if d.get("evidence") and f.get("evidence"):
            ef = self.m.fields.get("evidence", {})
            rows = []
            for e in d["evidence"]:
                if isinstance(e, dict) and ef.get("text"):
                    row = {ef["text"]: str(e.get("text"))}
                    if e.get("link") and ef.get("link"):
                        row[ef["link"]] = e["link"]
                    rows.append(row)
                else:
                    rows.append(e)
            body[f["evidence"]] = rows
        reply = self.door.call("raise", body)
        ident = reply.get(self.m.responses["item_id"])
        if not ident:
            raise InvalidRequest("raise: the reply names no item")
        return ident

    def _revise(self, ident, d, key, decision_key=None, note=None):
        self._need("revise")
        f = self.m.fields["revise"]
        body = {f["idempotency"]: self._uuid_of(key)}
        if _text_of(d) and f.get("text"):
            body[f["text"]] = _text_of(d, decision_key, self.m.data.get("options", {}).get("key_marker"))
        if d.get("recommendation") and f.get("recommendation"):
            body[f["recommendation"]] = str(d["recommendation"])
        if d.get("options") is not None and f.get("options"):
            body[f["options"]] = self._options(d["options"])
        if len(body) < 2:
            raise InvalidRequest("revise: nothing to change")
        self.door.call("revise", body, ident=ident)

    def _withdraw(self, ident, d, key, decision_key=None, note=None):
        self._need("withdraw")
        self.door.call("withdraw", {self.m.fields["withdraw"]["reason"]: str(note or d.get("reason") or "withdrawn")},
                       ident=ident)

    def _status(self, ident, d, key, decision_key=None, note=None):
        if "status" not in self.m.operations:
            return   # the other side has no such operation: the tier change or note stays local (no "status" capability)
        f = self.m.fields.get("status", {})
        body = {f["idempotency"]: self._uuid_of(key)} if f.get("idempotency") else {}
        for name in ("status", "tier"):
            if d.get(name) is not None and f.get(name):
                body[f[name]] = d[name]
        if note and f.get("note"):
            body[f["note"]] = str(note)
        self.door.call("status", body, ident=ident)

    # ------------------------------------------------------------ events

    def _role(self, by):
        if isinstance(by, str):
            by = {"value": by}
        if isinstance(by, dict):
            for rule in self.m.data.get("roles", []):
                if all(by.get(k) == v for k, v in rule["when"].items()):
                    return rule["role"]
        return "unknown"

    def _answer(self, obj):
        spec = self.m.responses["answer"]
        generic = self.m.to_generic("disposition", obj.get(spec["disposition"])) or obj.get(spec["disposition"])
        meaning = dict(DEFAULT_MEANING, **self.m.enums.get("meaning", {}))
        by = _pick(obj, spec.get("by"))
        return {"id": obj.get(spec["id"]), "disposition": generic, "meaning": meaning.get(generic),
                "option_id": _pick(obj, spec.get("option_id")), "text": _pick(obj, spec.get("text")),
                "rationale": _pick(obj, spec.get("rationale")), "clarification": _pick(obj, spec.get("clarification")),
                "rejection": _pick(obj, spec.get("rejection")),
                "by": {"role": self._role(by), "raw": by}}

    def _normalise(self, change):
        spec = self.m.responses["change"]
        raw_kind = change.get(spec["kind"])
        kind = self.m.to_generic("change_kind", raw_kind)
        seq = change.get(spec["seq"]) if spec.get("seq") else None
        seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else None
        ident = change.get(spec["item_id"])
        event_id = str(seq) if seq is not None else hashlib.sha256(
            json.dumps(change, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        event = {"event_id": event_id, "external_id": ident, "type": "other", "seq": seq,
                 "at": _pick(change, spec.get("at")), "raw_kind": raw_kind}
        if kind in ("answered", "reversed"):
            container = self.m.responses["answer"].get("container")
            source = change.get(container) if container else change
            if not isinstance(source, dict):
                return event
            answer = self._answer(source)
            if kind == "answered" and answer["disposition"] == "clarify":
                event.update(type="sent_back", clarification=answer["clarification"], by=answer["by"])
                return event
            event.update(type=kind, answer=answer, by=answer["by"])
            if kind == "reversed":
                replaced = change.get(spec.get("replaces")) if spec.get("replaces") else None
                event["replaces"] = self._answer(replaced) if isinstance(replaced, dict) else None
        elif kind == "rated":
            tier = self.m.to_generic("tier", change.get(spec["tier"])) if spec.get("tier") else None
            floor = self.m.to_generic("tier", change.get(spec["floor"])) if spec.get("floor") else None
            event.update(type="rated", tier=tier, floor=floor)
        elif kind == "revised":
            event.update(type="revised", text=_pick(change, spec.get("text")),
                         awaits_rating=_pick(change, spec.get("awaits_rating")))
        elif kind == "withdrawn":
            event.update(type="withdrawn", reason=_pick(change, spec.get("reason")))
        return event


# ---------------------------------------------------------------- work items (HTTP only)


class WorkClient:
    """One external work item per worker assignment: put (idempotent create), patch (coalesced), finish, get, list.

    The other side records at most one update per item per interval, so updates are coalesced: the last state wins,
    nothing is sent faster than `work_interval`, an unchanged update is not sent, and a refusal with a wait keeps the
    pending state and defers it. State is in memory (a restart starts a fresh interval; a refusal then defers)."""

    def __init__(self, door, mapping, clock=time.monotonic):
        self.door, self.m, self.clock = door, mapping, clock
        self.interval = float(mapping.data["work_interval"])
        self.items = {}

    def _state(self, work_id, project):
        return self.items.setdefault(work_id, {"project": project, "last": None, "not_before": 0.0, "pending": {},
                                               "sent": {}})

    def _body(self, fields):
        spec = self.m.fields["work"]
        body = {}
        for name, value in fields.items():
            if value is None:
                continue
            if name not in spec:
                raise InvalidRequest("work: %r is not a mapped field" % name)
            if name == "state":
                value = self.m.enums["work_state"][value] if value in self.m.enums.get("work_state", {}) else value
            elif name == "end":
                value = self.m.enums["work_end"][value] if value in self.m.enums.get("work_end", {}) else value
            elif name == "progress" and isinstance(value, str) and spec.get("progress_text"):
                value = {spec["progress_text"]: value}
            body[spec[name]] = value
        return body

    def _result(self, reply):
        spec = self.m.responses.get("work", {})
        item = reply.get(spec["item"]) if spec.get("item") else reply
        recorded = reply.get(spec["recorded"]) if spec.get("recorded") else None
        return {"item": item, "recorded": recorded}

    def put(self, work_id, title, project=None, **fields):
        """Create the item (idempotent: the same id again changes nothing). Sent at once."""
        body = self._body(dict(fields, title=title))
        reply = self.door.call("work_put", body, ident=work_id, project=project, force="http")
        state = self._state(work_id, project)
        state["last"] = self.clock()
        state["sent"].update(fields, title=title)
        return self._result(reply)

    def patch(self, work_id, project=None, **fields):
        """Update the item. Coalesced: returns {"sent": True, ...} or {"sent": False, "deferred": True, "due_in": s}
        (or "unchanged"); the last state wins. `flush` sends what became due."""
        state = self._state(work_id, project)
        merged = dict(state["pending"])
        merged.update({k: v for k, v in fields.items() if v is not None})
        changed = {k: v for k, v in merged.items() if state["sent"].get(k) != v}
        if not changed:
            state["pending"] = {}
            return {"sent": False, "unchanged": True}
        state["pending"] = changed
        return self._send_due(work_id, state)

    def _send_due(self, work_id, state):
        now = self.clock()
        wait = max(state["not_before"] - now, 0.0)
        if state["last"] is not None:
            wait = max(wait, state["last"] + self.interval - now)
        if wait > 0:
            return {"sent": False, "deferred": True, "due_in": wait}
        fields = state["pending"]
        try:
            reply = self.door.call("work_patch", self._body(fields), ident=work_id, project=state["project"],
                                   force="http")
        except RateLimited as error:
            state["not_before"] = self.clock() + max(error.retry_after, 1.0)
            return {"sent": False, "deferred": True, "due_in": max(error.retry_after, 1.0)}
        state["last"] = self.clock()
        state["sent"].update(fields)
        state["pending"] = {}
        result = self._result(reply)
        result["sent"] = True
        return result

    def flush(self):
        """Send every pending update that is due. {work_id: result} for those that were attempted."""
        done = {}
        for work_id, state in list(self.items.items()):
            if state["pending"]:
                result = self._send_due(work_id, state)
                if result.get("sent") or not result.get("deferred"):
                    done[work_id] = result
        return done

    def finish(self, work_id, end, summary, project=None, **fields):
        """End the item (final). Pending updates are dropped: the end carries the last word. Sent at once."""
        body = self._body(dict(fields, end=end, summary=summary))
        reply = self.door.call("work_finish", body, ident=work_id, project=project, force="http")
        self.items.pop(work_id, None)
        return self._result(reply)

    def get(self, work_id, project=None):
        return self._result(self.door.call("work_get", ident=work_id, project=project, force="http"))

    def list(self, project=None):
        reply = self.door.call("work_list", project=project, force="http")
        spec = self.m.responses.get("work", {})
        return reply.get(spec["items"], []) if spec.get("items") else reply


# ---------------------------------------------------------------- building one from a mapping file


def build_adapter(mapping_path, environ=None, sleep=time.sleep, clock=time.monotonic):
    """A McpRestAdapter from the mapping file at `mapping_path` (the key source runs on `start()`)."""
    return McpRestAdapter(MappingFile.load(mapping_path), environ=environ, sleep=sleep, clock=clock)
