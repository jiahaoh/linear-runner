---
kind: blocked
author: runner
headline.needs-decision: {subject} is paused because {cause}, and it needs your decision to continue.
headline.technical-block: {subject} is paused because {cause}, and it needs you to choose a recovery.
headline.environment: {subject} is paused by a host or service problem ({cause}), and it needs you to fix it and relaunch.
headline.runner-defect: {subject} is paused by a runner error ({cause}), and it needs you to check the runner before relaunching.
cause.worker_blocked: the worker reported that it cannot finish
cause.review_blocked: the independent review did not accept it
cause.checks_failed: checks still fail after the allowed repairs
cause.delivery_failed: the delivery packet failed its integrity checks
cause.budget_exceeded: the {phase} phase went over its soft budget
happened.worker_blocked: The worker ended its {phase} session with status blocked instead of ready.
happened.review_blocked: The independent review of the committed work was not accepted, so nothing was published.
happened.checks_failed: The configured checks still fail after the allowed repairs.
happened.delivery_failed: Delivery stopped before the independent review.
happened.budget_exceeded: The {phase} phase used more than its soft budget, or reported no usage, so the runner stopped it.
happened.other: The runner stopped at the {step} step with this message: {error}.
needed.worker_blocked: Decide whether to clarify the criterion, give the worker a note, or set the issue aside.
needed.review_blocked: Decide whether the reviewer is right. You can re-run only the review (with a note or a clarified criterion), resume the worker, or set the issue aside.
needed.repair_blocked: Decide whether the failing checks are right: if their configuration or environment was wrong, fix it and re-run the checks; to let the worker try again, give it a note; or set the issue aside.
needed.checks_failed: Look at the failing check logs; if their configuration or environment was wrong, fix it and re-run the checks, otherwise set the issue aside.
needed.delivery_failed: Look at the delivery packet, fix the renderer or its inputs, then re-run delivery and review.
needed.budget_exceeded: Decide whether the phase may use more; the recovery records the new allowance.
needed.needs-decision: Look at what changed outside the runner and decide how to reconcile it before resuming.
needed.technical-block: Look at the failure and decide how to recover.
needed.environment: Fix the host or service problem (for example refresh the Linear login or free disk space), then resume.
needed.claude-auth-token: The Claude token the runner uses (site claude.auth) was rejected or cannot be read. Regenerate it with claude setup-token, put it in the configured token file (mode 600, outside any Git repository) or variable, then resume.
needed.claude-auth-login: Claude Code's shared subscription login failed. Check it with claude auth status on the host and log in again if needed, then resume. Claude Code sessions on one host share that login and can race when it refreshes, so consider giving the runner its own long-lived token: claude.auth.oauth_token_file in the site configuration, created with claude setup-token.
needed.runner-defect: This looks like a runner bug rather than a problem with the issue. Check the supervisor log before resuming.
---
{headline}

{mention}

**What happened**
{what_happened}

**In their own words**
{own_words}

**What is needed**
{needed}

**To continue**
{continue_steps}

Evidence: {evidence}
