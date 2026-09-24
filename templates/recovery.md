---
kind: recovery
author: runner
headline.default: A {kind} recovery for {subject} recorded by {authorized_by} is being carried out now; no action is needed.
kind.resume: The runner resumes {subject} from its saved step ({step}).
kind.revalidate: The runner re-runs the checks for {subject} on the current source without a model and without using a repair slot; if they pass it commits and continues to the review, otherwise the normal repair loop applies.
kind.review: The runner re-runs only the independent review of the committed work.
kind.budget: The runner resumes {subject} with the new budget allowance for the {phase} phase.
kind.publish: The runner finishes publishing the already accepted review; no model runs.
drift.publish: The issue changed only outside the accepted criteria and scope, so the runner re-pinned it without a new review.
kind.defer: {subject} is set aside and the batch continues with independent issues.
then.continue: After this issue the batch continues.
then.stop: After this issue the batch stops again.
---
{headline}

**Recovery**
{action} The reason given was: {reason}

**Then**
{then}

**Owner note**
{note}

Evidence: {evidence}
