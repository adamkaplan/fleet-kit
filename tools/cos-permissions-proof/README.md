# Chief of Staff permission proof

Replays a corpus of shell commands through the real native OpenCode v2 `permissions:` rules of
`agents/opencode/chief-of-staff.md` and shows, per command, whether it ran, was denied, or was asked.

- `run.py`: starts a throwaway `opencode run --standalone` with its own HOME and XDG directories under a scratch directory
  (never your real config, never a background service), a scripted provider (`mock_provider.py`, no model involved), and a
  harmless shim for every command word that only logs `RAN <command>`. Nothing in the corpus executes for real.
- `corpus.json`: `allowed` (must run), `hostile` (must not run), and `harmless` (hostile-listed shapes that are in fact
  read-only or legitimate coordination, triaged by hand).
- Pass condition: every `allowed` command RAN; every `hostile` command was DENIED, except the listed `harmless` ones;
  **ASKED is 0**: the Chief of Staff's rules only allow or deny, they never prompt, so it can never block on a dialog.
  Exit status 1 otherwise.

```
python3 tools/cos-permissions-proof/run.py [--only allowed|hostile] [--out result.json] [--definition path]
```

Needs `opencode` v2 on PATH and python3. About 20 seconds for the whole corpus with 3 jobs.
Note: an agent definition with a `name:` key makes OpenCode v2 ignore the whole `permissions:` list; the proof would show everything running.
