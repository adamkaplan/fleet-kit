# .spire/box.json pre-check rules

The shape of box.json (`setup`, `when`, `run`, `refresh`) is defined by the box tool (`docs/BOX.md` of spire-land); this
file adds only the project's rules, checked by `tools/box_precheck.py` (the `box-json` land step):

- Types match what the box tool reads; step names are unique.
- `rewriteFlags` (top-level list in box.json) names the flags that make a command rewrite files. A step whose `run`
  uses one needs a `when` guard, so it never runs again with nothing to do.
