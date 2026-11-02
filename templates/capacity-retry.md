---
kind: capacity-retry
author: runner
headline.default: The {phase} turn for {issue} hit model capacity and will retry automatically; no owner action is needed.
---
{headline}

**What happened**
{failure}.

**Next**
Retry {retry} of {maximum} runs after {seconds} seconds with {model} at {effort} effort on {backend}, in the same phase and without using a repair or review slot. {session}

Evidence: {evidence}
