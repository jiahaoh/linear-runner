# Shared contract (example)

Every issue dispatched for this project follows these rules. Issue descriptions reference
this file instead of repeating it; the runner pins it by SHA-256 when an issue is taken, and
the reviewer assesses each issue's own criteria against this exact version.

## Evidence

* Record the starting revision, the commands you ran and their outcomes, and any limitation.
* Keep run outputs under the artifact directory named in the prompt, never in Git.
* Missing values are unknown, not zero; do not claim an unexecuted check passed.

## Scope and ownership

* Implement only the dispatched issue. The controller owns Linear, full checks, commits and
  publication.
* No push, merge, dependency installation or resource expansion unless the issue says so.

## Validation

* Run focused tests for what you changed. The controller runs the configured checks.
* Match numerical expectations to independent reasoning; do not invent thresholds.

## Reporting

* Owner updates are short plain sentences through the outbox drafts named in the prompt.
