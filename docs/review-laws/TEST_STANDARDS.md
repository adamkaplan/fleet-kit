extends: fleet-kit-review-laws @ review-laws-2

# TEST_STANDARDS

What the tests of a change must show. Each rule is numbered and short so a review can cite it. A repository edits this copy to add or override rules. A test reviewer reads
the tests and the change and judges them; it never runs them.

## Rules

- **R-TEST-1 New behaviour has a test first.** A change that adds or alters behaviour carries a test that states the behaviour; a change without one says why none is possible. _check: diff: new or changed behaviour and the test that states it._
- **R-TEST-2 A test asserts the behaviour.** A test that asserts nothing, or only that no error was raised, is not a test of the change. _check: diff: a test body with no assertion on the behaviour._
- **R-TEST-3 The proof must be able to fail.** A test fails on the previous code (or on a mutated version of the change); a guard that has never been seen failing is not evidence. _check: test: the test fails on the previous code or a mutation._
- **R-TEST-4 Do not test the mock.** A test that replaces the unit under test with a double, or asserts only what the double was told, proves nothing about the unit. _check: diff: a test whose only assertions are on a double._
- **R-TEST-5 Failure and edge paths.** The tests cover the failure paths and the boundaries (empty, maximum, repeated, concurrent) the change introduces, not only the happy path. _check: diff: failure and boundary cases for what the change introduces._
- **R-TEST-6 Tests state behaviour, not structure.** A test breaks when behaviour changes, not when the implementation is rearranged. _check: note only: a judgement about brittleness._
- **R-TEST-7 No flake.** A test does not depend on wall-clock time, ordering, the network or machine load; time and randomness are injected. _check: search: a real clock, sleep, network or ordering dependence._
- **R-TEST-8 No debt.** Duplicate tests and tests that cannot fail are removed; the proportion of test to change is reasonable for the risk. _check: note only: a judgement about proportion._
- **R-TEST-9 Fixtures are synthetic.** A fixture uses neutral names and invented data with the same shape as the real case; nothing private is committed. _check: search: a fixture holding a private name or real data._
- **R-TEST-10 The unchanged still holds.** A change states what must stay the same and has a test that shows it does (a golden output, a regression case). _check: test: a golden or regression case for what must stay the same._
