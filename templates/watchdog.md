---
kind: watchdog
author: runner
headline.gone: The supervisor for batch {batch} stopped without reporting an outcome while working on {subject}, and it needs you to check the host and relaunch.
headline.stalled: Batch {batch} has recorded no progress on {subject} for {minutes} minutes although its supervisor is still running, and it needs you to check it.
---
{headline}

{mention}

**What the watchdog saw**
{observed}

**Last update**
{last_update}

**To continue**
{continue_steps}

Evidence: {evidence}
