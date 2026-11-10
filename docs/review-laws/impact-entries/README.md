# Impact-matrix entries, one file per entry

The lists of `docs/review-laws/impact-matrix.json` that every new module extends live here, one file per entry, so two pull requests that each add an entry never edit the same line or file (ask #2206, part 3).

Layout: `<list>/<entry-name>`, where `<list>` is one of `top_tier_paths.<class>`, `floor.<repo>.globs`, `top_tier_outside_floor`, `authority_modules.exempt`, `closure_exempt` (when present). A file holds the entry's value (a path or glob; for `authority_modules.exempt`, the reason text). The ONE shared loader merges them into the same dict the JSON gave.

A new module adds its files here and edits no shared line.
