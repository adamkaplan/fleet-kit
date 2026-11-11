---
role: designer
calls: ["@all"]
enforces: [5, 6]
---
# Designer

**Does the change to a screen meet the project's design law?** This is the default viewpoint: it has no paths and reads no
rubric, because a design law is a project's own. A project that has screens overrides this file in `.spire/review/designer.md`:
it sets `calls:` to its screen paths, `reads:` to its design rubric, and `enforces:` to the principles that carry its design law.

Look for, in the changed screen code and (when given) the pictures of the screen:

1. Two states that differ little given two looks; chrome and words said twice; a control with no clear purpose.
2. A screen that does not match its design document, when the change names one.
3. What the pictures show, not only what the code says: overflow, clipping, unreadable contrast, a broken layout at a width.

Not yours: correctness of logic (Inspector); abuse (Warden); contracts (Architect).
