"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): baseline_adapters."""
import contextlib
import copy
from unittest import mock
from support.loader import sb, sc


HEARTBEAT_EVERY = 30 * 60  # the old timer's cadence, for the Q1 baseline


def with_baseline(scenario, baseline):
    """The scenario as the control plays it: itself, or the old timer heartbeat added to it."""
    if baseline is None:
        return scenario
    if baseline != "timer-heartbeat":
        raise NotImplementedError("the offline runner has no baseline %r" % baseline)
    played = copy.deepcopy(scenario)
    end = max(sc.duration_seconds(step["at"]) for step in played["steps"])
    beats = [{"at": "%ds" % at, "actor": "world", "do": "timer-heartbeat"}
             for at in range(HEARTBEAT_EVERY, end + 1, HEARTBEAT_EVERY)]
    played["steps"] = sorted(played["steps"] + beats, key=lambda step: sc.duration_seconds(step["at"]))
    return played


def baseline_assign_ids(items, repos=None):
    """ADAPTER: ids for items {seed, ask, agent, bare, since, repo}, in order (today sb.assign_decision_ids)."""
    return sb.assign_decision_ids(items, repos)


def baseline_headline(kind, title, issue_title=None):
    """ADAPTER: the headline of one decision of `kind` and `title`, for a report about an issue titled
    `issue_title` when known (today sb.headline_of)."""
    return sb.headline_of({"kind": kind, "title": title}, issue_title)


def baseline_attach_hub(engine, runner, now):
    """ADAPTER: give a daemon-style engine the GitHub picture the daemon's hub would hold, as `decisions --fresh`
    does for its own engine (today a GitHubHub filled by read_awaiting)."""
    hub = sb.GitHubHub(engine.config, engine.state_dir, None, runner, sb.spawn_forwarder, now, "cli")
    hub.engine, engine.hub = engine, hub
    engine.gh_runner = runner  # the notices' `gh pr view` reads
    for repo in sb.decision_repos(engine):
        hub.read_awaiting(repo, now())


@contextlib.contextmanager
def baseline_unreadable_inbox(session_id, failure):
    """ADAPTER: the inbox of one session cannot be read (today V2Client.inbox_list raises)."""
    real = sb.V2Client.inbox_list

    def read(self, sid, *args, **kwargs):
        if sid == session_id:
            raise RuntimeError("inbox read failed") if failure == "RuntimeError" else sb.V2Error("v2 says 500")
        return real(self, sid, *args, **kwargs)

    with mock.patch.object(sb.V2Client, "inbox_list", read):
        yield


def baseline_failing_derivation():
    """ADAPTER: the whole derivation raises (today sb.derive_decisions)."""
    return mock.patch.object(sb, "derive_decisions", side_effect=RuntimeError("boom"))


def baseline_derive(engine, now):
    """ADAPTER: what the derivation lists now and what it could not read, notices included: (decisions, errors)
    (today sb.derive_decisions, after a discovery and a fact collection)."""
    engine.discover(now())
    engine.collect_facts()
    return sb.derive_decisions(engine, now())
