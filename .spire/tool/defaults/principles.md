# Principles

The law of this project, written once. It is **yours**: it began as spire-land's default, and you change it as the project
learns (add a rule, rewrite one, delete one) by a landed change like any other. An update of the tool never overwrites it;
it only shows you what the new default changed. Builders (people, Spire specialists, agents)
read it before they build; every council reviewer judges against it; nobody keeps a copy.

A rule is cited by its number: **P3**. A review finding cites one; a rebuttal cites one (`--rebut "inspector=P3: why"`).
If you renumber, update the viewpoints in `.spire/review/` that name numbers (`enforces:`).

1. **One owner, one path, one definition.** Each thing has one owner, one way of being done, one definition of each state.
2. **Single source.** A fact or rule is written once. Anything an agent or a person reads is generated from that one source or
   points at it, never copied. A second copy is a defect, whatever its age.
3. **Run it; do not reason about it.** Only execution evidence shows that something works. The record says what happened,
   failures included.
4. **Reality outranks the guidelines.** When a real run contradicts a guideline, reality wins and the guideline is amended;
   nothing is hidden to keep the guidelines true.
5. **Simplicity first.** Do the least that works; every mechanism earns its place by a need shown. Cut the root, not the branch:
   ask whether a problem is one instance of a class before patching a symptom. Chesterton's fence: understand why something
   exists before removing it; existing code is not evidence of necessity, nor is it noise.
6. **The smallest change that leaves one way to do the thing.** A change does what it was asked and no more.
7. **Delete rather than update.** Code with no importer, a rule nothing enforces, a doc that restates another, a flag nobody
   passes: delete it. When something is replaced, the old one goes in the same change; no shim, no copy left to rot.
8. **Test what you changed.** A change carries the proof that would fail if it were wrong, and the tests of everything it
   touches run. Proof that cannot fail, or never reaches the changed line, is not proof.
9. **Small slices.** A landing is one whole small step that leaves main working. Large work is a series of such steps
   (`--slice N/M`), each landed on its own.
10. **No override, no skip.** Nothing lands past a red check or an open finding: there is no flag, variable, label or trailer
    that skips a test or a review. A refusal is a final answer, not an obstacle; the fix is to make the change right.
11. **Fail loudly.** An error is raised where it happens and says what is wrong and what to do; it is never swallowed,
    downgraded or retried into silence.
12. **The mechanism is product-agnostic.** The tool and this default law hold no fact about a product. What differs between
    projects is data in the project's own `.spire/` files and nowhere else.
13. **A finding cites a principle by number.** A reviewer's finding names the rule it serves (**P5**), a changed file and
    line, and why it matters. A claim that cites no rule is a note: it stops nothing. Reviewers never invent policy.
14. **A builder never implements advice it cannot ground** in a principle. It reports the advice and asks.
15. **A finding that contradicts a principle is a proposal to the owner.** It is raised, labelled a proposal, never dropped
    and never blocking: the owner decides the rule, then reviewers follow it.
16. **The law that judges a change is the law on main.** A change cannot rewrite the principles or the viewpoints it is
    judged by; it is judged by the base's. Change the law in its own change, argued as such.
17. **Fix what you find.** A red you meet, in your path or not, is fixed in your landing or in its own small landing, and you
    keep moving: nobody waits for the owner and nothing is reported as someone else's problem. Only a real bug in a person's
    change goes to that person.

## Rules in force (this project's own)

Rules that can change with the product and are this project's alone. Add them here, numbered on from the last principle
above (18, 19, ...) so a finding can cite them the same way. spire-land ships none.
