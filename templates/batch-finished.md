---
kind: batch-finished
author: runner
status: DRAFT
headline.complete: Batch {batch} finished: all {total} issues are Done, so no action is needed.
headline.partial: Batch {batch} finished with {done_count} of {total} issues Done; {owner} needs to decide what to do with the rest.
headline.checkpoint: Batch {batch} stopped at a planned checkpoint after {checkpoint}; {owner} can review it and relaunch when ready.
headline.stopped: Batch {batch} stopped between issues because a STOP marker was present; {owner} can relaunch when ready.
---
{headline}

**Issues**
{issues}

**Usage**
{usage}

**To continue**
{continue_steps}

Evidence: {evidence}
