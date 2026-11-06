"""The `--pr-label` option of the cloud launch (ask #2269, slice A): a part of bin/cloud_worker.py (it keeps that file within its size row).

A launch may name labels (repeatable) for the pull requests its pod opens. They go to the launcher's `create` as `--pr-label LABEL` (the launcher puts
them in the pod's CW_PR_LABELS and an annotation, and its reaper labels what the pod recorded) and into the worker record as `pr_labels`.
"""

OPTION = "--pr-label"


def labels_of(args):
    """The launch's labels as a list of strings (empty when the option was not given), each once, in the order given."""
    out = []
    for label in getattr(args, "pr_label", None) or []:
        if isinstance(label, str) and label not in out:
            out.append(label)
    return out


def create_options(args):
    """The launcher's create options for the labels: `--pr-label L` each; nothing when none were given (a launch hands the launcher exactly what it did)."""
    out = []
    for label in labels_of(args):
        out += [OPTION, label]
    return out


def record_labels(record, args):
    """Write `pr_labels` (a list of strings) into the worker record when the launch named labels; otherwise the record is left as it was."""
    labels = labels_of(args)
    if labels:
        record["pr_labels"] = labels
