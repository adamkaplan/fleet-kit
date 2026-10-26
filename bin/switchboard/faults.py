# ------------------------------------------------------------------ the faults

# Each fault switches one safeguard off, so a scenario's control can prove the
# safeguard is what makes the scenario pass. They work only in the Lab.
KNOWN_FAULTS = (
    "no-engagement-gate",
    "no-recheck",
    "no-dedupe",
    "steer-not-queue",
    "no-batch",
    "no-intent-header",
    "no-classify",
    "crash-after-send",
    "no-policy-judge",
    "judge-loosens",
    "no-maxims",
    "no-worker-source",
    "no-note-conversion",
    "no-import-metadata",
    "idle-wakes",
    "no-rider",
    "no-nudge-once",
    "no-fail-open",
    "ignore-reports",
    "no-description",
    "no-decisions-file",
    "stale-as-fresh",
    "decision-resolves-never",
    "static-repos",         # PR 15: the watched repos are read once, from the config
    "bare-issue-number",    # PR 15: an `owner/repo#N` qualifier is ignored
    "one-account",          # PR 15: every repo uses the default token
    "repo-create-allowed",  # PR 15: the judge's deterministic rule on `gh repo create` is off
    "key-missing-silent",   # PR 16: `status` does not say that no decision-model key is configured
    "no-ensure-on-use",     # PR 16: a command that needs the daemon does not start it
    "shared-profile-default",  # PR 16: `install` writes into the directory the ordinary OpenCode reads, by default
    "key-env-only-unseen",  # PR 16: `status` does not warn that the key is in the shell's environment only
    "config-read-once",     # PR 16: the daemon ignores an edit to its config file until it is restarted
    "catch-up-blocks-pass",  # PR 16: GitHub reads have no time budget or cap, so a big catch-up holds the pass
    "catch-up-on-fresh-start",  # PR 16: a repo with no read marker reads the whole look-back window
    "full-window-every-time",   # PR 16: a forwarder repo reads the whole look-back window on every catch-up
    "orders-start-only",
    "orders-wake",
    "anyone-can-order",
    "orders-never-judged",
    "tier-flat",               # PR 18: every decision is shown to you, whatever its tier
    "cos-orders-unfed",        # PR 18: the judge ignores the Chief of Staff's orders file
    "anyone-edits-cos-orders",  # PR 18: no agent is stopped from writing the Chief of Staff's orders file
    "answer-unjudged",         # PR 18: `decisions answer` and `decisions resolve` skip the judge
)

LAB_ENV = "FLEET_SWITCHBOARD_LAB"
FAULT_ENV = "FLEET_SWITCHBOARD_FAULT"


class FaultError(Exception):
    """A fault name nobody defined. A typo here would make a control prove nothing."""


def active_faults():
    """The fault names requested in the environment, checked, as a sorted list."""
    raw = os.environ.get(FAULT_ENV, "")
    names = sorted({part.strip() for part in raw.split(",") if part.strip()})
    unknown = [name for name in names if name not in KNOWN_FAULTS]
    if unknown:
        raise FaultError("%s names unknown fault(s): %s (known: %s)"
                         % (FAULT_ENV, ", ".join(unknown), ", ".join(KNOWN_FAULTS)))
    return names


def fault(name):
    """True only in the Lab, and only for a fault named in the environment."""
    if name not in KNOWN_FAULTS:
        raise FaultError("unknown fault: %r" % name)
    requested = active_faults()
    return os.environ.get(LAB_ENV) == "1" and name in requested


class SimulatedCrash(BaseException):
    """The `crash-after-send` fault: the process stops here, as if killed.

    A BaseException, so no per-agent `except Exception` in the pass catches
    it, and AuditLog.external lets it through without writing a result: a
    crashed process writes nothing after the crash. Scenario R2 is built on it.
    """
