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
  medium (named only), for every profile and phase (a `Research` entry in `registry/pools.json`). Before, Standard and Economy Research
  issues used the `*` pools (gpt-6-astra medium; gpt-6-luna max). Project impact: none; a
  paused batch needs `recover repin-config`. Canary tier: canary batch (models).

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
