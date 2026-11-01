"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): import_fakes."""
import importlib.machinery
import importlib.util
import sys
import importlib
from support.paths import BIN, REPO
from support.delivery_measure import REPO
from support.paths import BIN
from support.policy_and_import import V1_FIXTURES


FAKE_IMPORT_FIELDS = {
    "body": {"info", "messages", "location"},
    "info": {"id", "parentID", "fork", "projectID", "agent", "model", "cost", "tokens", "outcome", "time", "title",
             "subpath", "metadata", "permissions", "revert", "location"},
    "user": {"id", "metadata", "time", "text", "files", "agents", "skills", "type"},
    "assistant": {"id", "metadata", "time", "type", "agent", "model", "content", "snapshot", "finish", "rawFinish",
                  "providerState", "cost", "tokens", "error", "retry"},
    "text": {"type", "text", "state"}, "reasoning": {"type", "text", "state", "time"},
    "tool": {"type", "id", "name", "executed", "providerState", "providerResultState", "state", "time"},
}


def _unknown_fields(item, kind):
    return ["%s: %s" % (kind, name) for name in sorted(set(item) - FAKE_IMPORT_FIELDS[kind])]


def _fake_message_problems(message):
    kind = message.get("type") if isinstance(message, dict) else None
    if kind not in ("user", "assistant"):
        return ["message of type %r (the fake models user and assistant)" % kind]
    problems = _unknown_fields(message, kind)
    if not str(message.get("id", "")).startswith("msg_"):
        problems.append("message id must start with msg_")
    if not _is_ms(message.get("time", {}).get("created")):
        problems.append("message time.created must be a number")
    if kind == "user" and not isinstance(message.get("text"), str):
        problems.append("user text must be a string")
    for part in message.get("content", []) if kind == "assistant" else []:
        ptype = part.get("type")
        if ptype not in ("text", "reasoning", "tool"):
            problems.append("content of type %r" % ptype)
        else:
            problems.extend(_unknown_fields(part, ptype))
        if ptype == "tool" and part.get("state", {}).get("status") not in ("streaming", "running", "completed", "error"):
            problems.append("tool state %r" % part.get("state"))
    if kind == "assistant" and not all(isinstance(message.get(k), t) for k, t in (("agent", str), ("model", dict), ("content", list))):
        problems.append("assistant needs agent, model and content")
    return problems


def _is_ms(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def fake_import_problems(body):
    """Everything the fake v2 refuses in an import body."""
    if not isinstance(body, dict):
        return ["body must be an object"]
    problems = ["body: " + n for n in sorted(set(body) - FAKE_IMPORT_FIELDS["body"])]
    info, messages = body.get("info"), body.get("messages")
    if not isinstance(info, dict) or not isinstance(messages, list):
        return problems + ["info and messages are required"]
    problems.extend(_unknown_fields(info, "info"))
    for name in ("id", "projectID", "cost", "tokens", "time", "location"):
        if name not in info:
            problems.append("info.%s is required" % name)
    if not str(info.get("id", "")).startswith("ses"):
        problems.append("info.id must start with ses")
    if not _is_ms(info.get("time", {}).get("created")) or not _is_ms(info.get("time", {}).get("updated")):
        problems.append("info.time needs created and updated numbers")
    if not isinstance(info.get("location", {}).get("directory"), str):
        problems.append("info.location.directory must be a string")
    for message in messages:
        problems.extend(_fake_message_problems(message))
    return problems


def _fixture_path(name):
    path = (REPO / "scenarios" / name).resolve()
    if (REPO / "scenarios").resolve() not in path.parents:
        raise ValueError("a scenario fixture must live under scenarios/: %s" % name)
    return path


V2_SHAPE = V1_FIXTURES / "v2-export-shape.json"


ALERT_BAD_SHAPES = {   # one sample per row of sb.ALERT_SHAPE_CHECKS: (the whole triage section, the rows that may fire)
    "not-a-map": {"alert_labels": ["ops-alert"]},
    "bad-repo": {"alert_labels": {"widget": ["ops-alert"]}},
    "labels-not-a-list": {"alert_labels": {"acme-org/widget-service": "ops-alert"}},
    "bad-label": {"alert_labels": {"acme-org/widget-service": [""]}},
    "duplicate-label": {"alert_labels": {"acme-org/widget-service": ["ops-alert", "ops-alert"]}},
    "repo-not-read": {"repos": ["acme-org/other-service"], "alert_labels": {"acme-org/widget-service": ["ops-alert"]}},
}


def load_judge_eval():
    loader = importlib.machinery.SourceFileLoader("judge_eval_under_test", str(BIN / "judge-eval"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# ---- #1215: the test files of the impact brief are collected by THIS runner (CI runs `bin/test-switchboard`; a file no runner executes does not count)
def _load_brief_tests(name):
    """The test module of the impact brief, loaded by path; every TestCase it defines is named below (an explicit binding, not a write to this module's globals), and the check after them fails when one is missing."""
    sys.path.insert(0, str(REPO))   # `tests.*` and the bin modules they import
    sys.path.insert(0, str(BIN))
    if name == "test-impact-brief":
        loader = importlib.machinery.SourceFileLoader("brief_test_impact_brief", str(BIN / name))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
    else:
        spec = importlib.util.spec_from_file_location("brief_" + name, REPO / "tests" / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module
