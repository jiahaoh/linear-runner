# Changelog

Each release is a Git tag `v<version>` on the release commit, and `linear_runner/version.py`
names it. Every entry states two things for project owners:

* **Project impact:** `none`, or the exact configuration change a project, site, workspace or
  batch file needs, with the `validate-config` (or launch) message that asks for it.
* **Canary tier:** `first-issue checkpoint` (the change touches only preflight, reporting or
  documentation: launch the next batch with `--stop-after` its first issue) or `canary batch`
  (the change touches the engine, prompts, backends or models: run a canary batch on low-risk
  issues before the next production batch).

A paused or stopped batch adopts any new runner commit with `recover repin-config`, whatever
its tier. The release procedure is in the README ("Releases").

## Unreleased

### Fixed

* The `"last_issue": "when_changed"` rule (2.4.0) compares against the whole batch. The
  history of finished issues did not record their starting commits, so the batch's base
  revision was always the last issue's own starting commit, and a check was skipped at the
  last issue although an earlier issue had changed its inputs. In s28-impl-20261002 two
  extended checks were skipped that way and the review of `W-297` blocked on the missing
  evidence. History entries now record `starting_commit`. A batch state written by 2.4.0 or
  2.4.1 needs no edit: the base is read from the first finished issue's run manifest, or,
  without it, is the parent of that issue's first controller commit. Project impact: none.
  Canary tier: canary batch (engine) (`W-302`).

## v2.4.1 — 2026-10-02

Codex workers no longer inherit network access from the host's Codex configuration.

**Project impact:** none, unless a project's worker relied on network access inside the Codex
sandbox. A paused batch adopts the release with `recover repin-config`.

**Canary tier:** canary batch; the Codex backend's worker argv changed.

### Changed

* Codex workers run with `-c sandbox_workspace_write.network_access=false`. The worker sandbox
  (`--approve-for-me`, workspace write) reads the host's Codex configuration, so a host that
  enables network access there for interactive use also gave it to every worker; the runner
  now sets it off on fresh and resumed worker calls. Read-only review calls are unchanged.
  Project impact: none, unless a project's worker relied on network access inside the sandbox.
  Canary tier: canary batch (backend argv).

## v2.4.0 — 2026-10-01

The findings of the §2.7 batches (s27-spec-20260930, s27-impl-20261001, s27-notebook-20261001
and s27-rename-20261001, all complete; `W-282`). The four batches took about 18 hours: 6 of
them in 15 validation rounds, and 6 of the 11 unplanned pauses had runner or process causes.

**Project impact:** none; the new check fields `timeout_seconds` and `last_issue` and the
recovery option `--append-issues` are optional. The registry gains `max_check_timeout_seconds`,
which changes configuration fingerprints: a paused batch adopts the release with `recover
repin-config`. Check evidence saved by an earlier runner is not reused once, because its key
changed.

**Canary tier:** canary batch; the engine (contract fields, review coverage, check selection,
evidence reuse and time limits) and the recoveries (`repin-config --append-issues`) changed.

### Added

* Check `timeout_seconds`: a project check may set its own time limit, up to the registry's
  new `max_check_timeout_seconds` (7200); a check without one keeps `check_timeout_seconds`
  (1800). A check that exceeds its limit is stopped and fails with exit code 124 and a note.
  `W-274`'s validation module needed about 2217 s at one thread and had to be split by test
  selection. `validate-config` lists every check's tier, limit and last-issue rule under
  `checks`. Project impact: none; optional. The registry gains a key, which changes
  configuration fingerprints: a paused batch adopts it with `recover repin-config`. Canary
  tier: canary batch (engine).
* Check `last_issue`: an extended check with `"last_issue": "when_changed"` runs at the
  batch's last issue only if a file changed since the batch's base revision matches its
  `inputs`. Otherwise it is recorded as `status: "skipped"` with its reason, counts as
  passing, is listed under "Not applicable" in the validation comment and is no evidence for
  a required check. Before, the last issue ran every extended check whatever its inputs: a
  notebook (`W-275`) and a rename (`W-277`) each paid for 36 minutes of detector validation
  per round. Without the option nothing changes. Project impact: none; optional. Canary
  tier: canary batch (engine).
* `recover repin-config --append-issues`: a paused, checkpointed or complete batch adopts
  issues added at the end of its allowlist, with a new `terminal_issue`. An inserted,
  reordered, removed or replaced issue is still refused, and so is an appended issue without
  the flag. The record names the appended issues; earlier history, usage and evidence stay,
  and the earlier completion record is dropped so the batch reports complete again. Before,
  `W-275` and `W-277` each needed a batch, branch and worktree of their own. Project impact:
  none. Canary tier: canary batch (recovery and supervisor).

### Changed

* The `blocks` relation is no longer part of the issue contract; `blockedBy` and
  `duplicateOf` still are. `blocks` mirrors another issue's `blockedBy`, so creating an issue
  that waits for a running one changed the running issue's contract: `W-270` stopped before
  its review and needed `recover review --repin-contract`, with its description and criteria
  unchanged. The check before a review now logs a `blocks` change and continues. State pinned
  by runner 2.1.0 to 2.3.0 verifies under the earlier formula, so a paused batch continues.
  Project impact: none. Canary tier: canary batch (engine).
* Review coverage is judged on the pinned wording after aligning copy marks: a returned
  criterion that differs from a pinned one only by Markdown code or emphasis markers,
  backslash escapes, Linear issue-mention markup, typographic quotes or ellipses, or
  whitespace covers it, gets the pinned wording, and the returned text is logged. The second
  review of `W-273` returned `ready` with all nine criteria satisfied but had dropped two
  pairs of backticks, and the batch paused with "Reviewer omitted original checklist criteria
  (1 missing; 9 returned)". A missing, reworded, repeated or unexpected criterion still
  fails; pinned criteria that differ only by such marks keep exact matching. Project impact:
  none. Canary tier: canary batch (engine).

### Fixed

* Check evidence survives a relaunch. The evidence key hashed the supervisor's whole
  inherited environment, including `INVOCATION_ID` and the other variables systemd sets anew
  for every unit. Every recovery needs a new launch, so after a recovery every check ran
  again even when nothing had changed: both `W-266` repairs changed no worktree file and
  still reran the default checks. The key now leaves those variables out. It is also taken
  after a round of checks has run, because checks write ignored files that broad `inputs`
  match (a test cache, generated documentation sources) and the first round's key was stale
  at once. Project impact: none; saved evidence from an earlier runner is not reused once.
  Canary tier: canary batch (engine).

## v2.3.0 — 2026-09-30

The findings of the §2.6 batches (s26-spec-20260929, s26-impl-20260930, s26-notebook-20260930
and s26-direction-20260930, all complete; `W-263`).

**Project impact:** none; the new batch `variables` and workspace `auth.auto_refresh` fields are
optional. The implement output budget rises from 150k to 250k, and the input budget now judges
uncached input, which changes configuration fingerprints: a paused batch adopts the release
with `recover repin-config`.

**Canary tier:** canary batch; the engine (budget judgement, contract check before review) and
the Linear client (automatic credential refresh) changed.

### Added

* Batch `variables`: values for site `variables` that apply to that batch only, for example
  another Python environment for one batch's checks; the operator had to edit `site.json`,
  which switched every batch on the host. Each name must already be a site variable
  (executables, the built-ins and new names are refused, so a misspelt name fails). The
  values are in the configuration fingerprint and `validate-config` lists them under
  `variable_overrides`. Project impact: none; optional. Canary tier: first-issue checkpoint
  (configuration).
* Workspace `auth.auto_refresh` (default `false`): when the Linear client reads an expired
  Codex-owned credential, it runs `auth.refresh_command` once (argv, no stdin, a temporary
  directory, 180 s at most), rereads the credential and continues when the new expiry is in the
  future, logging one line with the old and new expiry, never the token. A failed, timed-out or
  ineffective refresh ends in the same "Linear OAuth expired" stop as before; each expiry value
  is tried at most once per process. The `linear_credential` preflight warning says the runner
  will refresh automatically. Project impact: none; optional (with `credentials_file` only).
  Canary tier: canary batch (the Linear client runs a command during the batch).

### Changed

* Soft budgets judge uncached input: the `input_tokens` limit is compared with `input_tokens`
  minus `cached_input_tokens` whenever the backend reports a cached figure, for a `ready` and a
  `blocked` phase alike (it was only a `ready` phase whose other figures were all within
  budget); without a cached figure the total input is judged as before. `phase-usage.json`
  records a `budget_note` whenever the total input was over the limit and the uncached input
  was not, and a stop for input names the uncached figure. The implement output budget is
  250k (was 150k); repair (50k) and review (40k) are unchanged. Three §2.6 implement
  checkpoints needed `recover budget` only for this: `W-245` (20.2M input, 19.8M cached,
  166,709 output, ready), `W-255` (15.9M, 15.7M cached, blocked) and `W-256` (31.1M, 30.7M
  cached, 183,191 output, blocked). The rule is in `registry/phases.json` notes. Project
  impact: none; the registry change changes configuration fingerprints, so a paused batch
  adopts it with `recover repin-config`. Canary tier: canary batch (engine and registry).

### Fixed

* The live issue contract is compared with the pinned one again immediately before every
  independent review (a first review, the fresh review after a repair, or `recover review`),
  not only when a launch starts. A difference stops the batch before the issue moves to review
  or a model runs, with a `needs-decision` stop (event `contract_changed`) that names the issue,
  what changed and the command `recover review --repin-contract`; the Linear stop comment
  offers it first. Before, a criterion edited in Linear while a `recover repair
  --repin-contract` ran was not noticed, the fresh review assessed the old wording and blocked
  again, and the operator needed `recover review --repin-contract`. When a pending `recover
  review` or `recover repair` meets a changed issue, the launch preflight now names what
  changed and gives `recover cancel` and the recovery with `--repin-contract`. Publication
  (`--accept-contract-drift`) is unchanged. Project impact: none. Canary tier: canary batch
  (engine, stop comment template).

## v2.2.1 — 2026-09-29

The findings of the 2.2.0 canary batches (canary-w224-20260929 and canary-w253-20260929, both
complete; `W-251`).

**Project impact:** none. A workspace that relied on the launch failing 30 minutes before the
Linear credential expires sets `auth.min_lifetime_minutes`. A paused batch adopts the release
with `recover repin-config`.

**Canary tier:** canary batch; the controller's commit message (engine) changed. The other
changes are the Linear client's retry of unavailable answers and preflight and README wording.

### Fixed

* A Linear answer that it is temporarily unavailable (a 5xx or `upstream_unavailable`) no
  longer stops the batch at once: reads and idempotent writes are repeated after 2 s and 5 s,
  and a comment write is repeated only after its marker is looked for, so a comment that did
  land is adopted and never posted twice. A failure that remains is an `environment` stop
  ("Linear temporarily unavailable"); it was a `technical-block`. The 2.2.0 canary paused on a
  502 while posting W-224's review comment. Project impact: none.
* Controller commit messages leave out the worker summary's sentences about commits ("Changes
  are uncommitted.", written before the controller committed), and a repair of review findings
  is titled `fix(<issue>): address review findings (<issue title>)` instead of the
  ungrammatical "address review findings on <title>". Project impact: none.
* README operator practice: do not change an issue's `blocks`, `blockedBy` or `duplicateOf`
  relations while a batch runs it; the 2.2.0 canary's relaunch was refused for that.

* The `linear_credential` preflight no longer tells the operator to refresh early "to start with
  a full lifetime": Codex refreshes the Linear credential only once it has expired, and running
  the refresh command earlier left the expiry unchanged in the 2.2.0 canary (`W-251`). The
  warning now names the expiry time and says to run the command when the batch pauses at it,
  then `recover resume`. `auth.min_lifetime_minutes` is off by default (was 30), because
  failing a launch early would only make the operator wait for the expiry. Project impact:
  none; a workspace that relied on the 30-minute failure sets `min_lifetime_minutes`. Canary
  tier: first-issue checkpoint (preflight).

## v2.2.0 — 2026-09-29

The fixes for the runner problems found in the §2.5 batches (`W-251`), the `W-236` emphasis
fix, descriptive controller commit messages and `runner/` batch branches; the runner release
for the start of §2.6.

**Project impact:** none. Existing configurations need no change. Optionally: set
`site.attention.notifier` to a `command` backend for unattended batches (README "Stops"), set
the workspace `auth.refresh_command`, `min_lifetime_minutes` or `warn_lifetime_minutes`, use
batch `phase_overrides` for a known long phase, and pin `"runner_version": "2.2.0"`. A paused
batch adopts the release with `recover repin-config`.

**Canary tier:** canary batch; the engine, prompts (draft rules) and the budget rule changed.
Run a small canary batch before the first §2.6 production batch.

### Added

* Launch preflight step `linear_credential`: the Codex-owned Linear OAuth credential's
  remaining lifetime and expiry, when its file records one. It fails below the workspace's
  `auth.min_lifetime_minutes` (30) and warns below `auth.warn_lifetime_minutes` (720);
  `launch` prints warnings under `warnings`. The stop for an expired credential now says when
  it expired and names the refresh command (`auth.refresh_command`, default `codex exec
  --skip-git-repo-check 'Reply with OK.'`) (`W-251`). Project impact: none; optionally set the
  three `auth` keys. Canary tier: first-issue checkpoint (preflight).
* Batch `phase_overrides`: `{"<issue>": {"<phase>": {"timeout_seconds": N}}}` raises a model
  phase's hard timeout for a named issue, up to the new registry `phases.max_timeout_seconds`
  (14400). `validate-config` refuses a larger value or an issue outside the allowlist and
  lists the overrides; each attempt's `session.json` records its `timeout_seconds` (`W-249`,
  `W-251`). Project impact: none; optional.
* Watchdog `paused` reminder: when the supervisor exited on a recorded stop and no recovery
  has been recorded for `attention.watchdog.paused_minutes` (60; `0` turns it off), the
  watchdog posts one reminder comment on the stopped issue and calls the notifier; the
  launch's timer keeps running until then. README "Stops" recommends a `command` notifier for
  unattended batches (`W-251`: one batch sat paused for about 10.5 h unnoticed). Project
  impact: none; set `site.attention.notifier` to a `command` to be notified off the host.
* README operator practice: one criterion per `- [ ]` line, no identifiers of issues that do
  not exist yet, precisely scoped batch guidance rules.

### Changed

* Batch branches are named `runner/<batch id>` (was `codex/`): the controller commits on them
  whichever backend implements. Documentation, the example batch and test fixtures only; the
  runner never enforced a prefix. Project impact: none; existing batches keep their branches.
* Controller commit messages say what the issue delivers: `feat(<issue>): <Linear issue title>`
  (cut to 72 characters), the worker's summary as the body and a `Linear-Issue: <issue>`
  trailer; a repair of review findings is `fix(<issue>): address review findings on <title>`.
  They were `feat(<issue>): implement validated issue deliverables` for every issue. Project
  impact: none. Canary tier: canary batch (engine).
* Soft budgets: a phase that returned `ready` is not stopped when only its input is over
  budget while its uncached input (`input_tokens - cached_input_tokens`) and every other
  figure are within budget; `phase-usage.json` records a `budget_note`. Resumed long sessions
  reread their context as cached input on every turn: four §2.5 pauses (`W-233` repair 6.25M
  with 6.21M cached, `W-249` implement 22.2M with 21.8M cached and repair 16.8M with 15.6M
  cached) needed `recover budget` only for that (`W-251`). The rule is in `registry/phases.json`
  notes. Project impact: none. Canary tier: canary batch (engine).
* An outbox draft whose only lint problem is its length is posted cut to fit, with a sentence
  naming the full draft file, instead of being replaced by the runner's fallback; the draft
  rules in every prompt say so (`W-241`, `W-251`). Project impact: none. Canary tier: canary
  batch (prompts).
* `recover` waits up to 60 s for the project lock a just-paused supervisor may still hold, then
  ends with one line and exit 2 (no traceback) telling the operator to wait and retry. A busy
  lock reported as EACCES (`PermissionError`, `flock` emulated on network file systems) is
  treated as busy, not as a crash (`W-251`). Project impact: none.

### Fixed

* Acceptance criteria keep their nested sub-items: lines indented below a `- [ ]` line are
  joined into its text (`; ` between items), for the intake packet, the review schema and the
  reviewer's verbatim list. Only the `- [ ]` line was kept, so `W-233`'s "Each recipe reports:"
  reached the reviewer without its list and the review blocked (`W-251`). Checked items stay
  excluded with their nested lines; one-line criteria are unchanged. Project impact: none; an
  active issue whose criteria have nested lines gets the joined text at its next review.
  Canary tier: canary batch (engine).
* Linear read-backs retry: the comment, review/in-progress state, needs-input label or state
  and publication read-backs read again after short pauses (about 10 s in all) before they
  fail, without repeating the write. Three §2.5 pauses came from reads taken before Linear
  showed a write it had acknowledged (`W-238`, `W-241`, `W-251`). Project impact: none.
* Publication read-back and `recover publish --accept-contract-drift` treat Linear's mention
  markup for an issue (`<issue id=… href=…/issue/W-242/…>W-242</issue>`, or a Markdown link
  to it) as equal to the plain identifier; a different identifier, or markup linking another
  issue, still mismatches. The raw `issue_contract` hashes are unchanged. `W-242` had to be
  deferred although it was accepted and Done, because Linear linked the plain "W-242" in the
  ticked description (`W-251`). Project impact: none; a batch paused at that stop adopts the
  fix with `recover repin-config`, then `recover publish`. Canary tier: canary batch (engine).

* Publication read-back, the lifecycle read-back and `recover publish --accept-contract-drift`
  ignore emphasis markers (`*`) outside code spans, as they already ignored `[x]`/`[X]`.
  Linear re-serializes the description the runner writes and may move or drop emphasis next
  to an issue mention; an accepted issue whose description had such a mention then stopped
  with "Published checklist read-back mismatch", and `recover publish` could not pass
  (`W-229`, `W-236`). Words, links, code spans, bullets and every other contract field still must
  match. Project impact: none; a batch paused at that stop adopts the fix with `recover
  repin-config`, then publishes with `recover publish`. Canary tier: canary batch (engine).

## v2.1.0 — 2026-09-26

Milestone 05 (runner–project decoupling), the Research model pools and two fixes. A canary
batch on three STARfinder issues (`W-214`) passed with this code before release.

**Project impact:** none. Existing configurations need no change. Optionally declare
`"interface_version": 1` in project profiles and batch files, and pin `"runner_version":
"2.1.0"` in a batch to refuse other checkouts. A paused batch adopts the release with `recover
repin-config`.

**Canary tier:** canary batch; prompts and models changed, and the canary in `W-214` passed.

### Added

* The runner-project interface version: `registry/interface.json` and
  `interface-migrations.json`. A project profile and a batch file may declare
  `interface_version`; `validate-config` refuses an older one with the migration steps and a
  newer one with a request to update the runner, and reports undeclared files (`W-205`).
  Project impact: none (declaring it is optional); declare `"interface_version": 1`.
* Every model prompt (worker and reviewer, both backends) states that the runner's task
  instructions take precedence over the repository's agent instructions on commits, Linear
  updates, checks and the handoff; README "Integrating a project" (`W-203`). Canary tier of
  this prompt change: canary batch.
* `runner.py sync-linear-template`: renders `templates/issue-contract.md` for a Linear issue
  template (`--dry-run` prints it) and checks a copy in the workspace, recording its ID
  (`issue_template`). No Linear copy is kept: `templates/issue-contract.md` stays the only
  maintained copy, because pasting into Linear turned the checklist criteria into plain
  bullets and dropped the rules block's language tag, and the Linear MCP endpoint cannot
  write templates (`W-206`, `W-213`). Project impact: none.

### Changed

* Research issues use gpt-6-astra high, then claude-opus-5-5 high, then claude-opus-5-5
  medium (named only), for every profile and phase (a `Research` entry in
  `registry/pools.json`). Before, Standard and Economy Research issues used the `*` pools
  (gpt-6-astra medium; gpt-6-luna max).

### Fixed

* Under Python older than 3.10 every command now stops at once with "linear-runner needs
  Python 3.10 or newer, but <interpreter> is Python <version>" instead of a traceback from
  deep in the configuration code. Project impact: none.
* While an active issue owns uncommitted work, launch preflight reuses an earlier baseline
  pass only if the configuration, environment and fixtures are unchanged since it; otherwise
  it records the step as skipped with the reason (`W-210` review). Project impact: none.

## v2.0.0 — 2026-09-26

The first versioned release. It covers the rebuilt runner (milestone 03), the multi-backend
runner with model pools (milestone 04), run summaries (`W-200`), `recover repair` (`W-209`),
the recovery fixes from batch `split-20260925` (`W-210`) and release versioning (`W-204`).

**Project impact:** none for configurations written for the rebuilt runner, except:

* a site whose batches can select a Claude pool entry must name the executable in
  `site.executables.claude`; `validate-config` otherwise says "site.executables.claude: a
  default pool entry or a batch model_overrides entry runs on the claude backend, so the site
  must name its executable";
* a batch `model_overrides` entry naming `gpt-5.6-luna` must name `gpt-6-luna`;
  `validate-config` otherwise says "batch <id>.model_overrides.<phase>: unknown model
  'gpt-5.6-luna'". An issue label naming it (`model:gpt-5.6-luna` and the phase labels)
  fails the launch preflight for that issue instead;
* a paused batch pinned by an earlier runner commit needs `recover repin-config`; any command
  otherwise says "Configuration/guidance changed since ... was pinned".

**Canary tier:** canary batch. Milestones 03 and 04 passed their canaries (`W-182`, `W-191`)
and `recover repair` ran in batch `split-20260925`; the `W-210` recovery changes have not yet
run in a batch.

### Added

* One engine and one configuration model: registry, private overrides, site, workspace,
  project and batch layers, validated offline by `validate-config` (milestone 03).
* `runner.py launch`: model-free preflight with identity-keyed reuse, a `systemd-user`
  supervisor and a watchdog timer; no outer model session (milestone 03).
* Plain-language Linear comments from templates, an outbox for worker drafts, stop
  notifications and the needs-input mechanism (milestone 03).
* Named, recorded recoveries with a hash-chained log: `resume`, `revalidate`, `review`,
  `repair`, `budget`, `publish` (with `--accept-contract-drift`), `defer`, `cancel` and
  `repin-config` (milestones 03–04, `W-209`).
* A Claude Code backend next to Codex, ordered per-task model pools, and a start check of
  each selectable backend in the worktree during preflight (milestone 04).
* A run summary table after Done and when an issue is set aside, and usage tables in the
  batch comment (`W-200`).
* `runner.py --version`; the release is recorded in `resolved-config.json`; a batch may pin
  `runner_version` (`W-204`).

### Fixed

* Baseline checks no longer run over a resumed active issue's own uncommitted work
  (`W-210`).
* `recover cancel` works after the configuration changed, so it no longer deadlocks with
  `repin-config` (`W-210`).
* A ready implement or repair phase stopped only by its soft budget keeps its result
  (`W-210`).
* `recover repair --repin-contract` adopts a clarified criterion without a separate review
  round (`W-210`).
