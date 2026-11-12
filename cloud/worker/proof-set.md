# The proof set of a worker image (ask #2418)

`bin/cloud_image_proof.py DIGEST` launches one proof pod on exactly that digest and has it run this set. The pod writes ONE results file,
`/work/proof-results.txt`, one line per item: `<item> pass` or `<item> fail`, using the six item names below and nothing else. The command reads
that file by exec; an item that is missing, misspelled or not exactly `pass` makes the verdict `fail`. Nothing here prints a secret: the identity item
reports only `pass` or `fail`.

| Item | What the pod checks, and writes `pass` only when it holds |
|---|---|
| `pg-env` | the count of environment variables whose name starts with `PG` is 0 (`env \| grep -c '^PG'` prints 0) |
| `hook` | `git config --global core.hooksPath` names a directory and the file `pre-push` is present and executable in it |
| `python311` | `python3.11 --version` or `mise exec python@3.11 -- python --version` runs and prints `3.11` |
| `run-wake` | a `cw-run start --name proofwake -- sh -c 'exit 13'` job wakes the agent within 30 seconds with no prompt (the wake arrives, a prompt is `fail`) |
| `tests` | each of the repository-named test files below, run in a clone of that repository, exits 0 (one line each; any red one is `fail`) |
| `identity` | `git config --global user.name` and `user.email` equal the two lines of `/etc/cw-default-identity` (compared, never printed) |

## The formerly red test files of the spire-venue repository

One line each; the `tests` item is `pass` only when every one exits 0.

- tests/queue-install.test.ts
- tests/queue-install-postgres.test.ts
- tests/index-facts-postgres.test.ts
- tests/demo-checkpoint-repos.test.ts
- tests/workroom-clone-real-git.test.ts
- tests/workroom-writer-git-env.test.ts
- tests/workroom-contract.test.ts
- tests/pod-setup-script.test.ts

## Changing the set

The set changes only by a reviewed pull request that edits this file and the item list in `bin/cloud_image_proof.py`; no flag skips an item.
