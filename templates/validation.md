---
kind: validation
author: runner
status: DRAFT
headline.passed: Checks passed for {issue} ({checks}); the runner is committing and will start the independent review, so no action is needed.
headline.repairing: {failed} of {total} checks failed for {issue}; the runner is starting repair {repair} of {max_repairs}, so no action is needed yet.
---
{headline}

**Failing checks**
{failing}

**Repair**
{repair_note}

Evidence: {evidence}
