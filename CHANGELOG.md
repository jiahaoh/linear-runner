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
