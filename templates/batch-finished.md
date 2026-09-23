---
kind: batch-finished
author: runner
headline.complete: Batch {batch} finished: all {total} issues are Done, so no action is needed.
headline.partial: Batch {batch} finished with {done_count} of {total} issues Done, and the rest need your decision.
headline.checkpoint: Batch {batch} stopped at a planned checkpoint after {checkpoint}; you can review it and relaunch when ready.
headline.stopped: Batch {batch} stopped between issues because a STOP marker was present; you can relaunch when ready.
---
{headline}

{mention}

**Issues**
{issues}

**Usage**
{usage}

**To continue**
{continue_steps}

Evidence: {evidence}
