---
name: fleet-explainer
description: Write the why-block of a pull request description. Use when you open or update a PR (every PR gets a before and after why-block, and no summary diagram), or when you are the explainer agent asked to write them.
---

# The PR explainer

The explainer is **not a reviewer**: it explains, once, for a person who has not read the diff. It is **read-only** (it never writes a
file or a comment), has a **50-turn limit**, and its output is pasted into the PR description by the author.

## What goes in the description

One block, between `<!-- pr-explainer:begin -->` and `<!-- pr-explainer:end -->`, right after the lines the body opens with
(`PLAN:`, `MERGE:`, `canon:`):

1. **Every PR: the why-block**, a short ```text block with exactly two lines: `before: ...` the behaviour as it was, `after: ...` the
   behaviour now. Behaviour, not code: what a person or a caller sees.

## Held pull requests

A PR held for the owner opens, right after the opening lines and in this order, with:

1. `## Why this needs you`: one to three sentences naming the hold reason.
2. `## What changes for you`: a `Before | After` table of 2 to 4 rows, each cell what a person sees or does.
3. `## Example`: one concrete case in a few lines.
4. Optional: a real screenshot of the actual screen, card or message, before and after. Never a box-and-arrow diagram.
5. `## Questions for you`: a numbered list, each ending `Recommendation: ...`; or the one line `Questions: none (reason)`.

The rest of the body follows. In the top any diagram fails; the summary flowchart fails anywhere in a held body. `pr-explain --check` and gate `G6b` name each missing part.

## The command

`fleet-switchboard pr-explain <owner/repo#N> --before "..." --after "..."` reads the PR once and PRINTS the block.
`--body-file F` (or `-` for the PR's own body) prints the whole new body with the block in **once**: a block already there is replaced.
Write it with your own `gh pr edit <n> --body-file ...`. `--check` judges a PR's description: exactly one block, a real before and
after, and no generated summary flowchart.

## Diagrams

A Mermaid diagram appears only where it shows data flow, system design or component design, never at the top of a body and never as a summary of the change. It goes below the top sections; the four-box summary flowchart (`Changes:`, `Touches:`, `Irreversible:`, `Risk:`) fails `--check`.

## What the explainer must not do

No advice, no review findings, no "consider", no restating the PR's own text, no second block, no image or link inside the block. If the before or the after cannot be said in two lines, say what is unclear; do not pad.

## janus-infra repositories (spire land)

When your setup text says "This repository lands with `spire land`" there is no pull request and no explainer block: commit, run it DETACHED: `nohup sh -c '.spire/tool/bin/spire land > /work/land.log 2>&1; r=$?; { echo; echo "exit $r"; } >> /work/land.log' > /dev/null 2>&1 &`, then poll with short calls (`sleep 120; tail -n 20 /work/land.log`, each under 5 minutes) and report `fleet-switchboard report working "land step <where it is>"` about every 10 minutes; never one foreground `spire land` call (it is moved to a background shell and your turn ends unwoken). The run is over when the log's last line is `exit <code>`: report `landed <sha>` or the exit code and what stopped it; exit 22 = held for a person: stop and say so. Without that line, this skill stands.
