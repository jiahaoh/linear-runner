---
author: person
status: approved (W-181); write every dispatched issue description in this shape
used-for: writing a Linear issue description that the runner dispatches; never posted by the runner
shared-contract: the project's contract_file (see README "Context cost")
---
## Purpose

<One or two sentences: what this issue changes and why it matters now. Link the parent or
tracking issue instead of restating it.>

Shared contract: `<contract file name>` as pinned by the batch configuration. It holds the
rules every issue follows (evidence and manifest, runtime and resource limits, stop
conditions, controller ownership, reporting style). Do not repeat them here; add a line
below only where this issue differs from the contract.

## Deliverables

* <A file, command, report or behavior that will exist when the issue is done.>
* <One line each; name paths or commands where they are known.>

## Acceptance criteria

The reviewer assesses exactly these unchecked items, verbatim, against the shared contract.
Write about 5 to 10. Each one can be checked from the diff, a command's output or a saved
file; say which when it is not obvious.

- [ ] <Observable result 1, with the command or file that shows it.>
- [ ] <Observable result 2.>
- [ ] <Observable result 3.>
- [ ] <Observable result 4.>
- [ ] <Observable result 5.>

## Decision rules (optional)

Leave this section out unless the batch should do something other than stop when this issue
blocks. Grammar: README "Decision rules".

```linear-runner-rules
defer issue when worker blocked 2 times on the same criterion
```

## Exclusions

* <What this issue must not do, when that is not already in the shared contract.>

## Context (optional)

* <Links to predecessor issues or files the worker should read on demand. The runner lists
  the project's context files by path and hash; do not paste their text here.>
