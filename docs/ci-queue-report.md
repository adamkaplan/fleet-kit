# The cause of the CI queue of 2 November 2026

The run of pull request #1927 sat queued more than 75 minutes. The cause is the shape of `.github/workflows/test.yml` against a hosted pool that is shared and small:

- every pull request run is 3 jobs (`plan`, `test (ubuntu-latest)`, `test-cloud (ubuntu-latest)`) or 5 with the two macOS legs; a push to main is 5; the nightly is 1 + 4 + 4 = 9;
- a `reopened` run (the gate pass's close and reopen, #1235) runs every leg, has its own concurrency group and is never cancelled (lines 25-27), so no push supersedes it;
- every push of a draft starts a run (the `pull_request` trigger has no draft filter), and a draft into `integration` still runs macOS;
- the pool is shared with the other repositories of the account, and macOS runners are the scarcest.

Where the numbers come from (read-only; `fleet-switchboard ci-queue`, GET requests only):

1. the Actions run list of the repository (`repos/<repo>/actions/runs`: `created_at`, `event`, `head_sha`);
2. the jobs API of each run (`repos/<repo>/actions/runs/<id>/jobs`: `created_at`, `started_at`, `completed_at`, `name`);
3. the commit of a head (`repos/<repo>/commits/<sha>`: the files), read against `.github/macos-platform-paths.txt` of the checkout the command runs in (`--paths-file`);
4. the account's plan limits: the settings the owner can see.

Not readable by a lane:

- the account's plan limits and billing (the concurrency cap, the minutes bill): not readable by a lane;
- the price of larger runners: not readable by a lane;
- the queue state of the other repositories on the same account (they share the pool): not readable by a lane;
- runs older than the retention window, and the hosted pool's own scheduling: not readable by a lane;
- the `reopened` action of a run and the draft state of a head at the time of the push (the run list does not carry them): not readable by a lane.

## The numbers of the 24 hours ending 2 November 2026, 16:20Z (measured by the command)

| Measure | Value |
|---|---|
| runs in the window | 448 (447 `pull_request`, 1 `schedule`) |
| jobs in the window | 1387 |
| longest queue wait of a run (created to first job started) | 157 minutes; the next are 122, 112 and 92 minutes (at least 75 minutes: the case of #1927 is in this range; the run list does not name the pull request of a run) |
| median and 90th percentile queue wait of a run | 311 s and 2909 s |
| job-minutes in the window, workflow `test` | 28847 |
| peak number of jobs queued at once | 70 |
| runner limit | "observed" ceiling of 61 concurrent jobs; published limit: "not readable by a lane" |

The ceiling is observed, not published: the highest count of jobs running at one instant of the window. It is set beside the "about 20" the cloud lane inferred; the published limit is to be filled by the owner.

## Candidates (job-minutes per day, counted over the same window)

| Kind | Candidate | Job-minutes per day |
|---|---|---|
| dedupe | 97 pull request runs on a head that already had a run (a repeat of a head: a `reopened` run or a rerun; the proxy for the `reopened` runs: the 17 reopens of 2 November are not readable from the run list) | 6580 |
| move | the macOS legs of 18 runs whose change touches no platform path (#990) | 1694 |
| cut | `nightly-cloud integration (macos-latest)` (no gate names it) | 100 |
| cut | `nightly integration (macos-latest)` | 62 |
| cut | `nightly main (macos-latest)` | 64 |
| cut | `nightly integration (ubuntu-latest)` | 55 |
| cut | `nightly main (ubuntu-latest)` | 58 |
| cut | other `nightly-cloud` legs (three) | 72 |
| dedupe | the duplicate checkout and setup of `test` and `test-cloud` (`fetch-depth: 0` twice per system) | not counted by a lane: the run list read does not carry step durations; the jobs API does carry them per job, but the command does not read them, so the checkout and setup job-minutes are not in the data |

The figure of S8 (the reopened runs and their job-minutes) is the tester's live measure: the command does not print a reopened-run count: it prints "repeat runs on one head" and "draft push runs" (the draft count is always 0, because the run list carries no draft flag), and the tester states the count it finds.

## The lane's own data for S8 and its source

A read of the repository's issue events API for 2 November 2026 UTC found 59 reopened events on 38 distinct pull requests (a pull request may be reopened more than once; the plan's earlier figure of 17 was an undercount); the job-minutes of their runs are NOT computed here: the tester's live measure makes them from the run list.

Draft-push runs: 0 counted (the run list carries no draft flag: not countable by a lane), 0 job-minutes.

Draft-push runs: 0 counted, because the run list does not carry the draft state. `plan` is read by no gate but builds the matrix: not a cut. The gate names are the four legs of `bin/merge_gate.py` (`UBUNTU_LEGS`, `MAC_LEGS`); whether the repository's own required checks add more is not readable by a lane.

Each change is a separate ask with its own plan: this ask changes no line of `.github/workflows/`.

## Spend options

Each is decided by the owner, through the Chief of Staff. No lane buys or enables one.

| Option | Price per month | Minutes saved per day |
|---|---|---|
| larger hosted runners | not readable by a lane (the owner reads the billing page) | queue wait of 2,322 run-minutes in the window (448 runs x a median of 311 s / 60), which is not job-minutes: the job-minutes a larger runner would save are not readable by a lane |
| more concurrency (a higher plan) | not readable by a lane | not readable by a lane (no figure of minutes derives from a peak of 70 queued jobs against a ceiling of 61) |
