# gh shim: wrapper chains (plan for #1758)

Status: plan only. The change edits a security shim and is held for the owner; no line of `shims/gh` is changed here.

"The last definition wins" is wrong for these names: each earlier definition is an inner link, bound to an alias that the
later definition calls. Deleting an earlier definition breaks the chain. Line numbers are those of `shims/gh` at the time of writing.

| Name | Links, in the order they run (outermost first) | Alias |
|---|---|---|
| `read_input_json` | outer (l.1452): `-` goes to the alias, a named file is opened non-blocking, must be regular, read as bytes; inner (l.1434): text parse | `_read_input_json_text` |
| `sign_plan` | outer (l.1668): turns an `Unsignable` of an `api` call into a `DeferredRefusal`; inner (l.1633): the signing | `_sign_plan_now` |
| `graphql_call` | outer (l.1778): copies query files to temp files, then calls the alias; inner (l.1540): the request-text decision | `_graphql_call_old` |
| `api_call` | outer (l.2139): `api` calls go to `api_decide(args, namer, alias)`; middle (l.1742): gated endpoints to `graphql_call`, else the alias; inner (l.1577): the REST comment shapes | `_api_call_round3`, `_api_call_now` |

## Collapse plan

1. Merge each chain into one `def` whose body inlines the inner link's code at the point the outer one called the alias.
2. Name every changed line with old and new text in the pull request (aliases removed, inner `def` lines removed).
3. Add a test that runs the old shim (from git) and the new one on the same fixed inputs (the `gh api`, `gh pr` and `gh issue`
   forms the shim tests use, plus the five forms of #1696) and compares output and exit status line by line: 0 differing lines.
4. State in the shim documentation that each function has one definition.
5. Hold the pull request for the owner; two strict security readers.
