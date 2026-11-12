# The proof set of a worker image (ask #2418)

`bin/cloud_image_proof.py DIGEST` launches one proof pod on exactly that digest and has it run this set. The pod writes ONE results file,
`/work/proof-results.txt`, one line per item: `<item> pass` or `<item> fail`, using the six item names below and nothing else. The command reads
that file by exec; an item that is missing, misspelled or not exactly `pass` makes the verdict `fail`. Nothing here prints a secret: the identity item
reports only `pass` or `fail`.

| Item | What the pod checks, and writes `pass` only when it holds |
|---|---|
| `pg-env` | the count of environment variables whose name starts with `PG` is 0 |
| `hook` | the global git configuration FILE names a hooks directory and the file `pre-push` is present and executable in it (read as text: git is never asked for the hooks path, which the pod's rules refuse) |
| `python311` | `mise exec python@3.11 -- python --version` runs and prints `3.11` |
| `run-wake` | a `cw-run` job that exits 13 wakes the agent within 30 seconds with no prompt (the wake arrives, a prompt is `fail`) |
| `tests` | each of the repository-named test files below, run alone in the checkout in `/work/repo/server`, exits 0 (any red one is `fail`) |
| `identity` | the global git configuration file's `name` and `email` values (read as text) equal the two lines of `/etc/cw-default-identity` (compared, never printed) |

## The commands of each item

Every command line below is one that no deny row of the pod's permission file refuses (a test matches each against every row). Use only these; each prints `pass` for a pass.

### pg-env

```
[ "$(env | grep -c '^PG')" = 0 ] && echo pass
```

### hook

```
d=$(sed -n 's/^[[:space:]]*hooksPath[[:space:]]*=[[:space:]]*//p' ~/.git[c]onfig | tail -n 1); [ -x "$d/pre-push" ] && echo pass
```

### python311

```
mise exec python@3.11 -- python --version | grep -q '^Python 3\.11' && echo pass
```

### run-wake

```
cw-run start --name proofwake -- sh -c 'exit 13'
```

Then END YOUR TURN (no other command, no waiting). The helper's wake message arrives by itself: when it does, and it says `proofwake` exited 13, write the line `run-wake pass`; no wake within 30 seconds, or a prompt, is `run-wake fail`.

### tests

The repository the pod was launched with must be the spire-venue repository these files belong to; its checkout is `/work/repo`. If `/work/repo` is absent, clone that repository there and check out its default branch first. Install the dependencies once, then run the eight files one at a time (each alone), each through `cw-run start` and `cw-run wait`; repeat a `cw-run wait` until it prints `exited`, and `exited 0` is a pass for that file. Two files need the ephemeral database wrapper, the others none. The `tests` item is `pass` only when all eight exit 0.

```
cw-run start --name proof-npm -- sh -c 'cd /work/repo/server && { [ -d node_modules ] || npm ci --ignore-scripts; }'
cw-run wait proof-npm 240
cw-run start --name proof-test-1 -- sh -c 'cd /work/repo/server && node --import ./tests/helpers/keep-event-loop-open.mjs --import ../scripts/dev/test-tmp-sandbox.mjs --test tests/queue-install.test.ts'
cw-run wait proof-test-1 240
cw-run start --name proof-test-2 -- sh -c 'cd /work/repo/server && /work/repo/scripts/dev/ephemeral-pg.sh -- node --import ./tests/helpers/keep-event-loop-open.mjs --import ../scripts/dev/test-tmp-sandbox.mjs --test tests/queue-install-postgres.test.ts'
cw-run wait proof-test-2 240
cw-run start --name proof-test-3 -- sh -c 'cd /work/repo/server && /work/repo/scripts/dev/ephemeral-pg.sh -- node --import ./tests/helpers/keep-event-loop-open.mjs --import ../scripts/dev/test-tmp-sandbox.mjs --test tests/index-facts-postgres.test.ts'
cw-run wait proof-test-3 240
cw-run start --name proof-test-4 -- sh -c 'cd /work/repo/server && node --import ./tests/helpers/keep-event-loop-open.mjs --import ../scripts/dev/test-tmp-sandbox.mjs --test tests/demo-checkpoint-repos.test.ts'
cw-run wait proof-test-4 240
cw-run start --name proof-test-5 -- sh -c 'cd /work/repo/server && node --import ./tests/helpers/keep-event-loop-open.mjs --import ../scripts/dev/test-tmp-sandbox.mjs --test tests/workroom-clone-real-git.test.ts'
cw-run wait proof-test-5 240
cw-run start --name proof-test-6 -- sh -c 'cd /work/repo/server && node --import ./tests/helpers/keep-event-loop-open.mjs --import ../scripts/dev/test-tmp-sandbox.mjs --test tests/workroom-writer-git-env.test.ts'
cw-run wait proof-test-6 240
cw-run start --name proof-test-7 -- sh -c 'cd /work/repo/server && node --import ./tests/helpers/keep-event-loop-open.mjs --import ../scripts/dev/test-tmp-sandbox.mjs --test tests/workroom-contract.test.ts'
cw-run wait proof-test-7 240
cw-run start --name proof-test-8 -- sh -c 'cd /work/repo/server && node --import ./tests/helpers/keep-event-loop-open.mjs --import ../scripts/dev/test-tmp-sandbox.mjs --test tests/pod-setup-script.test.ts'
cw-run wait proof-test-8 240
```

Expected (operator's run in a pod of image 48ecb532, all exit 0): queue-install 5 pass, queue-install-postgres 2 pass and 4 skipped, index-facts-postgres 13, demo-checkpoint-repos 3, workroom-clone-real-git 2, workroom-writer-git-env 6, workroom-contract 5, pod-setup-script 11.

### identity

```
[ "$(sed -n 's/^[[:space:]]*name[[:space:]]*=[[:space:]]*//p' ~/.git[c]onfig | tail -n 1)" = "$(sed -n 1p /etc/cw-default-identity)" ] && [ "$(sed -n 's/^[[:space:]]*email[[:space:]]*=[[:space:]]*//p' ~/.git[c]onfig | tail -n 1)" = "$(sed -n 2p /etc/cw-default-identity)" ] && echo pass
```

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
