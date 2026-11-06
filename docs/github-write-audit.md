# GitHub writes of the switchboard: the audit of each path (#2260)

Cause (6 November 2026, the strict read of #2251): the write helpers `_gh_write`, `_review_api`,
`write_issue_body` and `gh_call` audit nothing themselves; their callers may. The receipt written at
`bin/switchboard/plan_input.py` (about line 446) and the POST and PUT of `_review_api` had no audit line.
The old test pinned the number of write sites and so never proved auditing.

Every GitHub write path, and whether an audit line is written today (before this change):

| Write path | Writes | Audited today, by whom |
|---|---|---|
| `_gh_write` (non-GET) via `change_issue_labels` | label add (POST), remove (DELETE) | yes: `decisions.labels` by the caller (`_external`) |
| `_gh_write` via `comment_as` | issue comment POST | yes: `github.comment` by the caller |
| `_gh_write` in `charter_pane_update` | charter body PATCH | yes: `github.charter.pane` by the caller |
| `_gh_write` in `bin/lanes.py` move | sub_issues POST | yes: `lanes.move` by the caller |
| `_gh_write` in `answer_delivery.py` batch | issue create POST | yes: `decisions.batch` by the caller |
| `_gh_write` in `labels_ensure.py` | label create POST | yes: `labels.create` by the caller |
| `_gh_write` in `bin/impact_hold.py` (two) | PR body PATCH | no audit line around the write (the hold's own event is written later) |
| `_gh_write` in `branch_ff.py` | branch ref PATCH | no audit line around the write |
| `_review_api` POST | PR review post | no: `review_*_audit` is written after, only when the write succeeded |
| `_review_api` PUT | review dismissal | no: written after, only on success |
| `write_issue_body` | charter body PATCH (orders) | no (the orders caller audits some paths) |
| `gh_call` in `plan_input.py` ~446 | plan receipt comment POST | no |
| `gh_call` PATCH in `agents_verify.py` `edit_in_place` | comment edit | yes: `github.comment` by `audited(...)` of the caller |
| `post_ask_comment` in `handoff.py` | ask comment POST | yes: `github.comment` by `_external` |
| `merge_gate_gh` `gh` (merge, `rerun` close and reopen) | `pr merge`, PATCH state | no audit line at the write (the gate writes its own decision lines) |

`_gh_write` with GET (reads) is not a write. After this change every row above writes one line through
one helper (`github_write`, bin/switchboard/audit.py), before the write, and the write is not made when the line cannot be written.

Scope and limits:

- The `cloud_*` launcher files (`bin/cloud_*.py`) are out of the guard's scan; the plan for #2260 puts the launcher out of scope.
- In the merge gate, `gate_read_only` is an allow-list of reads (`pr view|diff|list|checks`, `run view|list`, `issue view|list`, `repo view`, a `gh api` without a method, field or input flag). Any other `gh` call through the gate is audited as a write, and no other call goes through the gate today. If a new read is added (for example `api graphql` or `pr status`), add it to `gate_read_only`.
- The verb of a `gh api` write is read from `-X X`, `-XX`, `--method X` and `--method=X`; with none it is POST.
