---
author: worker
used-for: the handoff file a worker writes before its session ends when bounded sessions are enabled; never posted to Linear
schema: schema/handoff.schema.json (linear-runner.handoff/1)
---
Write `handoff.json` in the attempt directory named in the prompt, as one JSON object.
Keep it under the byte limit given in the prompt: it is the only memory the next session
has besides the repository, the intake packet and the saved evidence, so write what that
session needs, not a diary.

* `schema`: `"linear-runner.handoff/1"`.
* `issue_id`: the dispatched issue, exactly.
* `status`: `in_progress`, `ready` or `blocked`.
* `summary`: two to four plain sentences: what exists now and what does not.
* `changed_files`: repository paths you created or changed (uncommitted).
* `criteria`: one entry per acceptance criterion, copied verbatim, with `state` `met`,
  `unmet` or `unknown` and a short `evidence` (a command and its outcome, or a file).
* `validation`: the commands you ran and their outcomes, for example `{"command": "python3 -m
  unittest tests.test_cli", "outcome": "12 passed"}`.
* `decisions`: choices the next session must keep, with the reason in a few words.
* `open_questions`: what you could not settle.
* `next_steps`: the first things the next session should do, in order.
* `evidence_paths`: saved logs or outputs worth opening, inside the artifact directory.

The controller keeps the issue identity, the shared repair limit and escalation, the frozen
source checks and usage attribution across the switch; the handoff does not change the
acceptance criteria or scope.
