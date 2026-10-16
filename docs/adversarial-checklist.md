# Adversarial checklist

This is the checklist for work that handles credentials, permissions or a privileged script. The coder designs against it and answers each item in one line in the pull request body. The strict reviewer (the independent reviewer of the pull request, not the coder's own panel) walks every item over the whole diff, in one message, so the failure paths are found together and not one review round at a time.

Answer each item in the PR body, in one line: a test name that drives it, or "not applicable because ...". A reviewer reads the answers, not the code, first.

**1. Every new branch that can fail**
- For each external call the change adds (a command, an HTTP request, a file write, a restart): what happens when it returns non-zero, times out, or returns an empty or odd answer? Is the failure path tested with a stub that does exactly that?
- Does any `set -e`, early return or exception skip a cleanup or rollback that a later line expects to run?
- If cleanup or rollback itself fails, what does the output and the audit say? Is "restored", "cleaned" or "done" printed only after the thing was checked?

**2. Every way the run can be stopped**
- Stopped by a normal signal (SIGTERM, Ctrl-C) halfway through: what state is left, and what recovers it?
- Killed with no chance to react (SIGKILL, pod or node loss) halfway through: what does the next start find, and does it recover or stay off?
- Is there any window where the guard is already off (or the new state already in place) but the proof that it is safe has not passed yet?

**3. Every loop and retry**
- Is each retry bounded by a count that survives a restart (not only a variable in memory)?
- Is each wait bounded by a clock, not a loop count?
- Can the failure path itself restart something expensive or dangerous in a loop (a restart loop, a crash loop, a retry storm)?

**4. Every run that can repeat or overlap**
- What happens when the run is replayed, runs twice, or overlaps another run? Is the second one a no-op?
- Is the shared state (a file, a lock, a marker, a cluster object) safe when two runs touch it at once?

**5. Every required input that can be missing**
- What happens when a required file, token or setting is missing or unreadable? Does the change refuse?
- Does it ever fall back to another credential when the intended one is missing? (It must not.)

**6. Every value that is a secret, and every form it can take**
- Where can the value appear: arguments, environment, files, logs, audit lines, exception text, command output, error messages echoed by another tool, the output of a `get` or `describe`?
- Which encodings can it appear in: base64 (standard, url-safe, padded or not, at each alignment), hex, percent-encoding, JSON escaping, a substring, a short value?
- Is there ONE scrub function every output passes through, and does the test plant a value of several lengths in every encoding?

**7. Every network call with a credential**
- Can it follow a redirect? To another host? To plain http? (It must not.)
- Which host does the credential go to? Is that host fixed in code, not taken from the response?
- What is the maximum size and time of an answer, and what happens at one over?

**8. Every parse of data you do not control**
- Wrong type, missing key, extra key, a list where an object is expected, a huge value, a number as a string, pagination, an empty page?
- Every pattern or glob you interpret: does it behave as the real system does (for example whether `*` crosses `/`), and does an unknown pattern fail closed?
- A name or path from outside: dot segments, empty segments, odd characters?

**9. Permissions and scope**
- Is the permission list compared as a whole (equality), not "contains"? What does one extra entry do?
- Is any rule "namespace-wide" or "all repositories" where one name would do? If it must be wide, is that stated in the PR and the README?
- Is the guard enforced where the action takes effect (the server, the token, the cluster policy), with any local wrapper only a convenience?

**10. What the change does NOT touch**
- Names and labels that belong to a platform or a third party (do not rename them); the real caller's identity in an audit line; settings that were explicit before (an explicit context): pinned by a test?
- A mechanical check: apply a rename table in reverse and diff against the base; list every residual.

**11. Evidence that is not a unit test**
- RULE: a privileged script is run live on a throwaway system (a forced failure, a forced hang and a kill) BEFORE its strict review, and the PR says which head's bytes ran.
- The orchestrator provides the throwaway system, or names it in the brief's authority line. With none, the PR says so.
- A label or marker another system sets (an installer, a controller): is it ever treated as proof of health? (It must not be.)

**12. What was not done**
- Say plainly what was not run (a path, a platform, a live check) and why.
