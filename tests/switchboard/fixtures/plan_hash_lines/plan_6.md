## Intent
Commissioning works.
Done when: (1) an owner can commission an agent; (2) it answers a chat message.

```yaml
scenarios:
  - id: S1
    covers: DW-2
    text: Given a project with 1 agents, when an owner commissions one, then it is listed
    proof: test
    pass: the list call returns status 200 and exactly 1 agent named "alpha"
    discriminates: a build that creates nothing returns an empty list and fails
    evaluator: ci
  - id: S2
    covers: DW-1
    text: Given a project with 2 agents, when an owner commissions one, then it is listed
    proof: test
    pass: the list call returns status 200 and exactly 2 agent named "alpha"
    discriminates: a build that creates nothing returns an empty list and fails
    evaluator: ci
  - id: S3
    covers: DW-2
    text: Given a project with 3 agents, when an owner commissions one, then it is listed
    proof: test
    pass: the list call returns status 200 and exactly 3 agent named "alpha"
    discriminates: a build that creates nothing returns an empty list and fails
    evaluator: ci
  - id: S4
    covers: DW-1
    text: Given a project with 4 agents, when an owner commissions one, then it is listed
    proof: test
    pass: the list call returns status 200 and exactly 4 agent named "alpha"
    discriminates: a build that creates nothing returns an empty list and fails
    evaluator: ci
  - id: U1
    covers: PC-1
    text: Given a project with 1 agents, when an owner commissions one, then it is listed
    proof: test
    pass: the list call returns status 200 and exactly 1 agent named "alpha"
    discriminates: a build that creates nothing returns an empty list and fails
    evaluator: ci
  - id: U2
    covers: PC-1
    text: Given a project with 2 agents, when an owner commissions one, then it is listed
    proof: test
    pass: the list call returns status 200 and exactly 2 agent named "alpha"
    discriminates: a build that creates nothing returns an empty list and fails
    evaluator: ci
```
