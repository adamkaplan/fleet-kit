# ============================================================================
# PR 7: the decision model, the classifier and the policy judge's questions
#
# One section, so the stack rebases cleanly. docs/switchboard.md, "Classify: a
# decision model" and "The judge". Registered in four places: the config check
# (`validate_jev_config`), `Switchboard.__init__` (`build_jev`), `run_pass`
# (`run_shadow_classifier`) and the unknown-key list (`PR7_CONFIG_KEYS`).
# ============================================================================
import socket
import urllib.error
import urllib.request

JEV_MODEL = "typesafe/jev-1.13"
JEV_URL = "https://openrouter.ai/api/v1/systemone"
JEV_KEY_ENV = "OPENROUTER_API_KEY"
JEV_DEFAULTS = {
    "model": JEV_MODEL,
    "url": JEV_URL,
    "key_env": JEV_KEY_ENV,       # the NAME of the variable that holds the key, never the key
    "key_file": None,             # PR 16: a private file that holds the key; None means jev.key beside config.json
    "timeout_seconds": 20,
    "min_confidence": 0.7,        # the classifier's threshold; SF2 will choose the real one
    "record_file": None,          # append every real call here as a request/response pair
    "replay": None,               # answer from this file instead of calling Jev
}
PR7_CONFIG_KEYS = ("jev", "shadow_judges")
JEV_HTTP_REASONS = {400: "invalid", 401: "unauthorized", 402: "no_credit", 403: "unauthorized",
                    413: "too_large", 422: "invalid", 429: "rate_limited"}
JEV_REASONS = ("invalid", "unauthorized", "no_credit", "too_large", "rate_limited",
               "timeout", "unreachable", "bad_response")
JEV_TEXT_LIMIT = 1000   # characters of any one message or intent sent to Jev
JEV_DETAIL_LIMIT = 160  # characters of a provider's text kept in an error


def validate_jev_config(config, raw, source="config"):
    """Check and complete `jev` and `shadow_judges`. Silent when `raw` has neither."""
    if not any(name in raw for name in PR7_CONFIG_KEYS):
        return
    jev = raw.get("jev")
    if jev is not None:
        config["jev"] = _checked_jev_settings(jev, source)
    shadow = raw.get("shadow_judges")
    if shadow is not None and not isinstance(shadow, bool):
        raise ConfigError("%s: 'shadow_judges' must be true or false, got %r" % (source, shadow))
    if shadow and jev is None:
        raise ConfigError("%s: 'shadow_judges' needs a 'jev' object: there is no decision model to ask" % source)
    config["shadow_judges"] = (jev is not None) if shadow is None else shadow


def _checked_jev_settings(jev, source):
    if not isinstance(jev, dict):
        raise ConfigError("%s: 'jev' must be an object, got %r" % (source, type(jev).__name__))
    unknown = sorted(set(jev) - set(JEV_DEFAULTS))
    if unknown:
        warn_unknown_config("%s: 'jev' contains key(s) this tool does not read: %s (known: %s)"
                          % (source, ", ".join(unknown), ", ".join(JEV_DEFAULTS)), 'jev', unknown, list(JEV_DEFAULTS))
    settings = dict(JEV_DEFAULTS, **jev)
    for name in ("model", "url", "key_env"):
        if not isinstance(settings[name], str) or not settings[name].strip():
            raise ConfigError("%s: 'jev.%s' must be a non-empty string, got %r" % (source, name, settings[name]))
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", settings["key_env"]):
        raise ConfigError("%s: 'jev.key_env' is the NAME of an environment variable, got %r "
                          "(never put the key itself in this file)" % (source, settings["key_env"]))
    if not re.match(r"^(https://|http://(localhost|127\.0\.0\.1)[:/])", settings["url"]):
        raise ConfigError("%s: 'jev.url' must be https (the key travels with it), got %r" % (source, settings["url"]))
    if not _is_number(settings["timeout_seconds"]) or settings["timeout_seconds"] <= 0:
        raise ConfigError("%s: 'jev.timeout_seconds' must be a positive number, got %r"
                          % (source, settings["timeout_seconds"]))
    if not _is_number(settings["min_confidence"]) or not 0 < settings["min_confidence"] <= 1:
        raise ConfigError("%s: 'jev.min_confidence' must be a number above 0 and at most 1, got %r"
                          % (source, settings["min_confidence"]))
    for name in ("record_file", "replay", "key_file"):
        value = settings[name]
        if value is not None and (not isinstance(value, str) or not os.path.isabs(value)):
            raise ConfigError("%s: 'jev.%s' must be an absolute path, got %r" % (source, name, value))
    if settings["record_file"] and settings["replay"]:
        raise ConfigError("%s: 'jev' cannot both record real calls and replay recorded ones" % source)
    return settings


# ------------------------------------------------------------------ the client

class JevError(Exception):
    """A Jev call with no usable answer. `reason` is one of JEV_REASONS."""

    def __init__(self, reason, detail=None, latency_ms=None):
        self.reason, self.detail, self.latency_ms = reason, detail, latency_ms
        super().__init__("%s: %s" % (reason, detail) if detail else reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _scrub_provider_text(text, key=None):
    """One short line of a provider's text with the key (and any bearer value) removed."""
    text = str(text if text is not None else "")
    if key:
        text = text.replace(key, "[key]")
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [key]", text)
    return _one_line(text, JEV_DETAIL_LIMIT)


def _is_probability(value):
    return _is_number(value) and 0 <= value <= 1


def check_answers(questions, answers):
    """`answers` (a dict by question id, or a list in question order) as a dict, or JevError.

    A missing answer, an out-of-range probability or an option nobody offered
    is a bad answer, never a reading.
    """
    if isinstance(answers, list):
        if len(answers) != len(questions):
            raise JevError("bad_response", "%d answer(s) for %d question(s)" % (len(answers), len(questions)))
        answers = dict(zip(questions, answers))
    if not isinstance(answers, dict):
        raise JevError("bad_response", "no answers")
    for name, question in questions.items():
        answer = answers.get(name)
        if not isinstance(answer, dict):
            raise JevError("bad_response", "question %s is not answered" % name)
        if question.get("type") == "noul":
            if not _is_probability(answer.get("noul")):
                raise JevError("bad_response", "question %s: noul is not a probability" % name)
            continue
        if answer.get("choice") not in question.get("criteria", {}):
            raise JevError("bad_response", "question %s: choice is not one of the options" % name)
        probabilities = answer.get("probabilities")
        if probabilities is not None:
            values = list(probabilities.values()) if isinstance(probabilities, dict) else [None]
            if not all(_is_probability(v) for v in values) or abs(sum(values) - 1) > 0.01:
                raise JevError("bad_response", "question %s: probabilities do not sum to 1" % name)
            if set(probabilities) - set(question["criteria"]):
                raise JevError("bad_response", "question %s: probabilities name an option nobody offered" % name)
    return answers


class KeyFileError(Exception):
    """The key file exists and may not be used. Names the path and the rule, never the key."""


def jev_key_file(settings):
    """Where the decision-model key file is: `jev.key_file`, else jev.key beside config.json."""
    given = (settings or {}).get("key_file")
    return Path(given) if given else config_path().parent / "jev.key"


def locate_jev_key(settings, environ=None):
    """(key, source): the key and where it came from, `environment`, `file`, or (None, `none`).

    The variable named by `key_env` wins. Otherwise the key file, which must be a regular file (not a
    link) that you own and nobody else can read or write, and must not be empty: a file the group or
    the world can read is refused with the reason, and the key is never part of that reason.
    Raises KeyFileError.
    """
    environ = os.environ if environ is None else environ
    name = (settings or {}).get("key_env") or JEV_KEY_ENV
    value = environ.get(name)
    if value:
        return value, "environment"
    path = jev_key_file(settings)
    try:
        info = os.lstat(str(path))
    except FileNotFoundError:
        return None, "none"
    except OSError as err:
        raise KeyFileError("cannot read the key file %s: %s" % (path, err.strerror or err)) from None
    if not stat.S_ISREG(info.st_mode):
        raise KeyFileError("the key file %s must be a regular file, not a link or a directory" % path)
    if info.st_uid != os.getuid():
        raise KeyFileError("the key file %s must be owned by you" % path)
    if info.st_mode & 0o077:
        raise KeyFileError("the key file %s can be read by others: run `chmod 0600 %s`, or save the key again "
                           "with `%s key set` (mode 0600, owned by you)" % (path, path, PROG))
    try:
        text = path.read_text().strip()
    except OSError as err:
        raise KeyFileError("cannot read the key file %s: %s" % (path, err.strerror or err)) from None
    if not text:
        raise KeyFileError("the key file %s is empty" % path)
    return text, "file"


def jev_secrets(config, environ=None):
    """The decision-model key as a list (empty when there is none or the file is refused): what redaction hides."""
    if not config.get("jev"):
        return []
    try:
        key, _ = locate_jev_key(config["jev"], environ)
    except KeyFileError:
        return []
    return [key] if key else []


def _usage_of(usage):
    """The numbers of a usage block under names the audit does not redact."""
    usage = usage if isinstance(usage, dict) else {}
    pairs = (("in", "input_tokens"), ("out", "output_tokens"), ("cost", "cost"))
    return {short: usage[name] for short, name in pairs if _is_number(usage.get(name))}


class JevClient:
    """The decision model over HTTPS (OpenRouter's System One endpoint).

    The key is read from the environment variable named in the config at the
    moment of each call. It is never stored on the client, written to a file,
    put in an argument list, or kept in an error. `opener` has urlopen's shape
    so tests inject a fake and never reach a network.
    """

    def __init__(self, settings, environ=None, opener=None, clock=time.monotonic):
        self.settings = settings
        self.environ = os.environ if environ is None else environ
        self.opener = opener or urllib.request.urlopen
        self.clock = clock

    def decide(self, state, questions):
        """{"model", "answers", "usage", "latency_ms"}. Raises JevError, with the latency."""
        started = self.clock()
        try:
            try:
                key, _ = locate_jev_key(self.settings, self.environ)
            except KeyFileError as err:
                raise JevError("unauthorized", str(err)) from None
            if not key:
                raise JevError("unauthorized", "no key in $%s or the key file" % self.settings["key_env"])
            body = {"model": self.settings["model"], "state": state, "questions": questions}
            out = self._post(body, key)
            answers = check_answers(questions, out.get("answers"))
        except JevError as err:
            err.latency_ms = int((self.clock() - started) * 1000)
            raise
        result = {"model": _scrub_provider_text(out.get("model") or self.settings["model"], key), "answers": answers,
                  "usage": _usage_of(out.get("usage")), "latency_ms": int((self.clock() - started) * 1000)}
        self._record(body, out)
        return result

    def _post(self, body, key):
        request = urllib.request.Request(
            self.settings["url"], data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                     "Accept": "application/json"})
        response = None
        try:
            response = self.opener(request, timeout=self.settings["timeout_seconds"])
            raw = response.read()
        except urllib.error.HTTPError as err:
            reason = JEV_HTTP_REASONS.get(err.code, "unreachable")
            raise JevError(reason, "HTTP %s %s" % (err.code, _scrub_provider_text(self._body_of(err), key)))
        except (socket.timeout, TimeoutError):
            raise JevError("timeout", "no answer in %ss" % self.settings["timeout_seconds"])
        except urllib.error.URLError as err:
            if isinstance(err.reason, (socket.timeout, TimeoutError)):
                raise JevError("timeout", "no answer in %ss" % self.settings["timeout_seconds"])
            raise JevError("unreachable", _scrub_provider_text(err.reason, key))
        except OSError as err:
            raise JevError("unreachable", _scrub_provider_text(err, key))
        finally:
            if response is not None and hasattr(response, "close"):
                response.close()
        try:
            out = json.loads(raw)
        except (TypeError, ValueError):
            raise JevError("bad_response", "the answer is not JSON")
        if not isinstance(out, dict):
            raise JevError("bad_response", "the answer is not an object")
        return out

    @staticmethod
    def _body_of(err):
        try:
            return err.read().decode("utf-8", "replace")
        except Exception:
            return ""

    def _record(self, body, out):
        path = self.settings.get("record_file")
        if not path:
            return
        line = json.dumps({"request": body, "response": out}, sort_keys=True) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(line)


class ReplayJev:
    """Answers from a hand-written or recorded file, never from the network.

    The file is a JSON object {"pairs": [...], "rules": [...]} or JSON lines of
    pairs (what `jev.record_file` writes). A pair is {"request": {state,
    questions[, model]}, "response": {answers[, model, usage]}} and matches an
    identical request. A rule is {"contains": text, "choice": {option: p},
    "noul": {question: p}, "error": reason} and matches a request whose
    "message" contains the text (a rule without "contains" matches anything);
    the first match wins. A question nobody answered is an error, loudly.
    """

    def __init__(self, path, model=JEV_MODEL):
        self.path, self.model = path, model
        self._pairs = self._rules = None

    def _load(self):
        if self._pairs is not None:
            return
        try:
            text = Path(self.path).read_text()
            try:
                data = json.loads(text)
            except ValueError:
                data = {"pairs": [json.loads(line) for line in text.splitlines() if line.strip()]}
            if "request" in data:  # a record file holding exactly one line parses as that pair
                data = {"pairs": [data]}
            pairs, rules = list(data.get("pairs") or []), list(data.get("rules") or [])
        except (OSError, ValueError, AttributeError) as err:
            raise JevError("bad_response", "cannot read the replay file: %s" % _scrub_provider_text(err))
        self._pairs = {_canonical({"model": p["request"].get("model", self.model),
                                   "state": p["request"].get("state"),
                                   "questions": p["request"].get("questions")}): p["response"] for p in pairs}
        self._rules = rules

    def decide(self, state, questions):
        self._load()
        response = self._pairs.get(_canonical({"model": self.model, "state": state, "questions": questions}))
        if response is None:
            response = self._by_rule(state, questions)
        answers = check_answers(questions, response.get("answers"))
        return {"model": str(response.get("model") or self.model), "answers": answers,
                "usage": _usage_of(response.get("usage")), "latency_ms": int(response.get("latency_ms") or 0)}

    def _by_rule(self, state, questions):
        message = state.get("message") if isinstance(state, dict) else None
        subject = message if isinstance(message, str) else _canonical(state)
        for rule in self._rules:
            if rule.get("contains", "") in subject:
                if rule.get("error") in JEV_REASONS:
                    raise JevError(rule["error"], "replayed")
                return {"answers": self._answers_of(rule, questions), "model": rule.get("model")}
        raise JevError("bad_response", "nothing recorded for this question")

    @staticmethod
    def _answers_of(rule, questions):
        answers = {}
        for name, question in questions.items():
            if question.get("type") == "noul":
                answers[name] = {"type": "noul", "noul": (rule.get("noul") or {}).get(name, 0.0)}
                continue
            options = list(question.get("criteria", {}))
            given = rule.get("choice") or {}
            probabilities = {option: given.get(option, 0.0) for option in options}
            total = sum(p for p in probabilities.values() if _is_number(p))
            if total > 0:  # a rule may name options this question lacks: scale what is left to 1
                probabilities = {option: p / total for option, p in probabilities.items()}
            top = max(options, key=lambda option: (probabilities[option], option == "current"))
            answers[name] = {"type": "choice", "choice": top, "probabilities": probabilities}
        return answers


def build_jev(config):
    """The decision model the config asks for, or None when `jev` is not configured."""
    settings = config.get("jev")
    if not settings:
        return None
    if settings.get("replay"):
        return ReplayJev(settings["replay"], settings["model"])
    return JevClient(settings)


# ----------------------------------------------------------------- the classifier

CLASSIFY_ID = "about"
CLASSIFY_INSTRUCTIONS = (
    "The Chief of Staff is an AI assistant working for one person, who has just sent a new message. "
    "Which of the following is the new message about: the work in progress, an ask somebody else is "
    "already working on, or something new?")


def _clip(text, limit=JEV_TEXT_LIMIT):
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def _ask_line(ask):
    ask = ask or {}
    line = _clip(ask.get("intent") or "no stated intent", 300)
    if ask.get("done_when"):
        line += " (done when: %s)" % _clip(ask["done_when"], 200)
    return line


def classify_options(current_ask, open_asks):
    """Option name -> its criterion: `current`, one per open ask by issue number, `new`."""
    options = {"current": "The message is about the work in progress now: %s." % _ask_line(current_ask)}
    own = str((current_ask or {}).get("issue"))
    for ask in open_asks or []:
        name = str(ask["issue"])
        if name == own:
            continue  # the current ask is `current`, not a second option
        options[name] = "The message is about ask %s (%s), which %s already owns." % (
            name, _clip(ask.get("intent") or "no stated intent", 300), ask.get("owner") or "someone")
    options["new"] = "The message is about something else: new work unrelated to the work in progress and to every open ask."
    return options


def classify_request(message, previous_message, current_ask, open_asks):
    """(state, questions) for one classification."""
    state = {"message": _clip(message), "previous_message": _clip(previous_message) if previous_message else None,
             "current_ask": {"issue": (current_ask or {}).get("issue"), "intent": _ask_line(current_ask)},
             "open_asks": [{"issue": str(a["issue"]), "intent": _clip(a.get("intent") or "", 300),
                            "owner": a.get("owner")} for a in open_asks or []]}
    question = {"type": "choice", "instructions": CLASSIFY_INSTRUCTIONS,
                "criteria": classify_options(current_ask, open_asks)}
    return state, {CLASSIFY_ID: question}


def _default_current(reason, extra=None):
    out = {"choice": "current", "probabilities": None, "confident": False, "reason": reason,
           "jev_choice": None, "model": None, "usage": {}, "latency_ms": None}
    out.update(extra or {})
    return out


def classify(jev, message, previous_message, current_ask, open_asks, min_confidence=0.7):
    """One Jev call: {choice, probabilities, confident, reason, ...}. Never raises on Jev's account.

    `confident` is true when the top probability reaches `min_confidence`. When
    Jev is unreachable, errs, or is unsure, the choice is `current`: the safe
    default changes nothing. `jev_choice` keeps what Jev said, for the audit.
    """
    state, questions = classify_request(message, previous_message, current_ask, open_asks)
    try:
        result = jev.decide(state, questions)
    except JevError as err:
        return _default_current("jev %s" % err.reason, {"latency_ms": err.latency_ms})
    answer = result["answers"][CLASSIFY_ID]
    probabilities = answer.get("probabilities")
    out = {"probabilities": probabilities, "jev_choice": answer["choice"], "model": result["model"],
           "usage": result["usage"], "latency_ms": result["latency_ms"]}
    if not probabilities:
        return dict(_default_current("jev gave no probabilities"), **out, choice="current")
    top = probabilities.get(answer["choice"], 0.0)
    if top < min_confidence:
        return dict(out, choice="current", confident=False,
                    reason="unsure: %s at %.2f is below %.2f" % (answer["choice"], top, min_confidence))
    return dict(out, choice=answer["choice"], confident=True, reason="confident: %s at %.2f" % (answer["choice"], top))
