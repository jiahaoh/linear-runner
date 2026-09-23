# Outer batch launcher

This procedure applies only to the outer launcher. An implementation worker must
continue its own dispatched issue until its bounded result is ready.

Inputs: an explicitly authorized issue allowlist, reviewed baseline/worktree,
batch file and private configuration home, resource limits, the registry profiles,
prerequisite/human approval evidence, and durable state/artifact locations.
Never infer authorization for a subsequent batch from completion of the prior one.

1. Read the batch contract and run `validate-config` offline. Verify clean
   worktree/branch, runtime, authenticated direct Linear access, model availability,
   data identities, and the site's supervisor settings. Run the controller's
   `dry-run` to read current gates and pin the resolved project/assignee IDs; check
   `resolved-config.json`. Do not change human approval records.
2. Start the controller once under the host's persistent process supervisor,
   with logs redirected to durable storage and the authorized `--max-issues`.
   Use the existing host service/process-launch procedure; do not create a
   periodic Codex task, model heartbeat or a new user task for supervision.
3. Confirm process startup and one initial state/log checkpoint. Return the PID
   or service identity, allowlist, configuration identity, local terminal-report
   location and terminal Linear issue, then end the outer turn.
4. The deterministic controller owns waiting, timeouts, checks, bounded recovery
   and terminal reporting. Do not remain active to sleep, poll, tail logs or
   narrate unchanged state. Host monitoring may notify only on terminal results
   or actionable blockers. Automatic re-entry into the same task is optional and
   requires a verified host-supported completion event; it is not assumed.
5. When the user requests results or a verified completion event arrives, read
   `terminal-report.json` and the Linear terminal comment once. Summarize outcome,
   evidence, limitations and any action needed. Never report a local result as
   confirmed Linear acceptance if synchronization is pending.

Reusable user prompt:

> Launch the authorized batch described by <Linear execution record> using
> <absolute reviewed batch file> and <private configuration home>. Verify startup, return its execution identity
> and terminal-report destinations, then finish this turn. Let the deterministic
> runner execute the queue. Report the final result when requested or when a
> supported completion event arrives. Do not supervise it with a standing model.
