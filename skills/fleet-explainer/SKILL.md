---
name: fleet-explainer
description: Write the why-block and the infographic of a pull request description. Use when you open or update a PR (every PR gets a before and after why-block; a captain PR also opens with a diagram), or when you are the explainer agent asked to write them.
---

# The PR explainer

The explainer is **not a reviewer**. Reviewers judge against the written laws and give no advice and no summaries; the explainer
explains, once, for a person who has not read the diff. It is **read-only** (it reads the PR and the checkout and never writes a
file or a comment), has a **50-turn limit**, and its output is pasted into the PR description by the author.

## What goes in the description

One block, between `<!-- pr-explainer:begin -->` and `<!-- pr-explainer:end -->`, right after the lines the body opens with
(`PLAN:`, `MERGE:`, `canon:`), so a gate that reads the first line still sees it:

1. **Captain PRs only: the infographic**, first in the block: a Mermaid flowchart of four labelled boxes: what changes, what it
   touches (grouped by top directory), what is irreversible, and the risk cell from the impact matrix
   (`docs/review-laws/impact-matrix.json`: severity x probability, or `Eliminated`, or `not rated yet`). GitHub renders it; no image.
2. **Every PR: the why-block**, a short ```text block with exactly two lines: `before: ...` the behaviour as it was, `after: ...` the
   behaviour now. Behaviour, not code: what a person or a caller sees, in plain words or two lines of pseudo-code.

## The command

`fleet-switchboard pr-explain <owner/repo#N> --before "..." --after "..."` reads the PR once (a GET) and PRINTS the block; it writes
nothing. For a captain PR add `--irreversible "..."` and the rating (`--severity Marginal --probability Occasional`, or `--eliminated`).
`--body-file F` (or `-` for the PR's own body) prints the whole new body with the block in **once**: a block already there is replaced,
never doubled. Write it with your own `gh pr edit <n> --body-file ...`. `--check` judges the description a PR has: exactly one block, a
real before and after (no `<placeholder>` left), and for a captain PR a diagram that parses.

## What the explainer must not do

No advice, no review findings, no "consider", no summary of the code's quality, no restating the PR's own text, no second block, no
image or link. If the before or the after cannot be said in two lines, say so in the PR and say what is unclear; do not pad.
