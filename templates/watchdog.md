---
kind: watchdog
author: runner
status: DRAFT
headline.gone: The supervisor for batch {batch} stopped without reporting an outcome while on {subject}; {owner} needs to check the host and relaunch.
headline.stalled: Batch {batch} has recorded no progress on {subject} for {minutes} minutes although its supervisor is still running; {owner} needs to check it.
---
{headline}

**What the watchdog saw**
{observed}

**Last update**
{last_update}

**To continue**
{continue_steps}

Evidence: {evidence}
