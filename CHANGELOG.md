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

* A launched batch joins the progress board (mod 0.3.2) by itself also when it was launched
  from another session or a terminal, when the board already holds a batch of the same
  project: a board that is its session's own asks the new command `runner.py launches --since
  <time>` every thirty seconds and adds those of the batches launched since it last asked. Before, only a launch through the board session's own shell tool joined, and
  every other batch had to be added with `/runner-board add`. `launches` reads the launch
  records under the site's `state_root` and writes nothing. Project impact: none. Canary tier:
  first-issue checkpoint (a new read-only command and the board mod only) (`W-561`).

* The progress board (mod 0.3.1) opens its pane by itself only in a session whose own board
  has batches: one that ran `/runner-board` or launched a batch, which also opens the pane
  then. Before, every session start opened it from the board saved last, so it appeared on
  each switch of session tab in the desktop app. Elsewhere `/runner-board` still shows the
  board saved last. Project impact: none. Canary tier: none needed (the board mod only)
  (`W-554`).

## v3.3.2 — 2026-10-28

A documentation-only change to a YAML schema no longer runs the extended checks (`W-659`). A
project lists such schemas in the new key `schema_documentation` (fnmatch patterns, like
`documents_read_by_tests`); a changed `.yaml` or `.yml` file that matches one is inert, rule
`schema-documentation`, when both sides give the same lines once blank lines, full-line comments
and the scalar values of the keywords `description`, `title` and `$comment` are removed
(`linear_runner/engine/inert.py`, `without_schema_documentation`). A keyword under a property map
(`properties`, `$defs` and the like) names a property and is compared; a trailing comment, a key,
a type, a default or an enum is a difference; a tab in the indentation is never inert. The runner
still needs no YAML library: the comparison works on lines, and anything it cannot read stays a
difference. Jiahao asked for it on 2026-10-28: in hk-l-guides-20261028 a description edit in
`workflow/schemas/config.schema.yaml` started 50 to 75 minutes of slow, learned and extended
tests, and section 2.13 will change the workflow schema often.

**Project impact:** none. A project that wants the rule sets `schema_documentation`; the key
enters the configuration fingerprint only when set, and a paused batch adopts it with `recover
repin-config`. Tests that read description text belong in a default check, which always runs.

**Canary tier:** canary batch; the engine's check selection changes.

## v3.3.1 — 2026-10-28

`gpt-6.1-sol` at high effort becomes a named implement and repair entry in the Maintenance
Standard pools and the `*` Deep pools of `registry/pools.json`. It is never a default: a batch
selects it with `model_overrides` (for example `{"issues": {"W-1": {"implement": "gpt-6.1-sol",
"repair": "gpt-6.1-sol"}}}`), and an escalation from it keeps it at the Deep pool's effort.
Jiahao asked for it on 2026-10-28 so that a documentation batch spends less Claude usage, with
Codex implementing and Claude reviewing. No code changes.

**Project impact:** none. A batch that names `gpt-6.1-sol` for implement or repair needs this
release; on 3.3.0 its preflight fails with the model outside that phase's pool.

**Canary tier:** canary batch; a model becomes available to the implement and repair phases.

## v3.3.0 — 2026-10-27

An extended check whose matched changes are all inert is not run (`W-605`). The controller
compares each changed file with the base revision itself (`linear_runner/engine/inert.py`): a
`.py` file is inert when its AST without docstrings is the same (comments never reach the AST),
and a `.md` or `.rst` file is inert when it lies outside the project's `documents_read_by_tests`,
a rule that applies only when the project sets that key. A file added, deleted, renamed or
changed in mode, and one that does not parse on either side, is never inert, nor is any other
format. Nothing a worker or reviewer wrote is an input. The base is the issue's starting commit;
at the last issue or a named issue (`check_overrides`), a `last_issue: "when_changed"` check
compares the batch's changes with the batch's base revision instead, after the issue's own. The
check is recorded in `checks.json` as `skipped` with `inert` (base, scope and each file's rule).
It counts as not applicable, is no evidence for a required check, and is not stored in the check
cache, so its previous record keeps its reuse and its place in the shortest-first order; evidence
keys stay byte-based. The validation comment lists it under "Not applicable" with the number of
files compared, and `report`, `measure` and the synthesized handoff call it "not run (only inert
changes to its inputs)". Default checks, `always` checks at the last issue, and checks of `kind:
"docs"` or `"presentation"` are not affected; a check opts out with `inert_changes: "run"`.
`validate-config` lists each extended check's `inert_changes` and the project's
`documents_read_by_tests`, and refuses `inert_changes` on a default check, `"skip"` on a check of
another kind than `code`, and a pattern that is absolute or contains `..`. In
hk-g-stages-20261026, W-591's docstring citation and a test-data README edit ran 39 minutes of
learned and spot-finding checks. README "Lifecycle" step 4 states the limits: code that reads
docstrings or source text, and documents a test reads that the project's list omits, can fail
on an inert change, and a test that only a `when_changed` check runs has no backstop within the
batch.

**Project impact:** no key is required. A project whose extended code check depends on
docstrings or comments (doctest, a linter, coverage) sets `inert_changes: "run"` on it. A
project that wants the documentation rule sets `documents_read_by_tests`, which changes its
configuration fingerprint. A batch that pins `runner_version` 3.2.0 must pin 3.3.0 to launch on
this release.

**Canary tier:** canary batch; the engine's check selection changes.

## v3.2.0 — 2026-10-22

Validation fails fast and runs short checks first (`W-552`). A validation runs its selected
default checks first, then its selected extended checks, each tier shortest first by the wall
time of the check's latest passing record in the batch's check cache; checks without one keep
configuration order after the others. When a default check fails, the remaining default checks
still run and every selected extended check is recorded as `skipped` with `skipped_after` and a
note instead of running; the validation goes to repair, and the next validation runs them as
usual. Such a record never counts as passing, is never reused, and is left out of the failing
checks in the repeated-failure test and the validation comment, which names the checks it held
back; reports call it "not run (a default check failed first)". At W-535 a 22-minute default
failure was followed by about 3.5 hours of extended checks before a one-line repair. The
selection rules (`inputs`, `last_issue`, `check_overrides`, base parity) are unchanged.

**Project impact:** none. A batch that pins `runner_version` 3.1.0 must pin 3.2.0 to launch on
this release.

**Canary tier:** canary batch. The release changes the engine's validation: run a canary batch on
low-risk issues before the next production batch.

## v3.1.0 — 2026-10-19

Five changes for the pre-§2.13 housekeeping (`W-526`): a new reviewer model for Standard and
Economy issues, larger implement and check limits, two check options for batches and projects,
and a review rule for owner decisions recorded in operator notes. Each was written in a Claude
Code session and its diff reviewed by Codex against the issue's criteria.

`gpt-6.1-sol` reviews Standard and Economy issues (`W-528`). The registry lists it (Codex,
efforts medium and high), and it is the first entry of the `*` Standard review pool (high)
and the `*` Economy review pool (medium, reached by the low-risk review rule). The earlier
defaults stay as named alternatives. The Deep and Research review pools and every implement
and repair pool are unchanged.

The implement phase's default output budget is 300,000 tokens (was 250,000), and a project
may give one check up to 10,800 s (`max_check_timeout_seconds`, was 7,200) (`W-525`). Four
large §2.12 implementations stopped at the old budget while finishing ready, and two check
selections came within reach of the old timeout. The repair and review budgets, the phase
timeouts and the default `check_timeout_seconds` (1,800) are unchanged.

A check may set `environment` and `unset_environment` of its own (`W-524`). They apply to that
check only, after the project's `check_environment` and the inherited environment, with
`${name}` substitution in `environment`. Both are part of the check's definition, so a change
reruns it and changes the configuration fingerprint; `validate-config` lists them under
`checks` and refuses a name both set and unset. The launch baseline and the base-parity run
pass them too: the keys a check runs with are now one list in the engine.

A batch may name extended checks to run at a named issue, as at its last issue (`W-523`):
`"check_overrides": {"<issue>": {"run": ["<extended check>"]}}`. At that issue a named check
is treated exactly as at the last issue: it runs whatever that issue changed (with
`last_issue: "when_changed"`, only if the batch touched its inputs), its evidence is keyed on
the worktree (`W-507`), and its record carries a `run_reason`. The key is
part of the configuration fingerprint; `validate-config` lists it and refuses an unknown
check, a default check and an issue outside the allowlist. Issues whose criteria cite an
extended check's log (W-519, W-487) no longer need an amended criterion.

The review applies an owner's decision or reading of a criterion recorded in an operator note
(`recover ... --note-file`), also while it awaits the owner's confirmation (`W-522`). The
review prompt says so and asks the reviewer to name each decision it applied under Concerns.
The implement and repair prompts keep "they do not change the acceptance criteria". At W-487 a
review refused three such readings, and the operator had to edit the criteria in Linear and
repin them.

**Project impact:** no configuration key changes; the new keys are optional.

* The site's `model_catalog` must offer `gpt-6.1-sol` at medium and high; otherwise intake
  stops before the claim with "Requested model/effort unavailable in host CLI catalog". A
  long-running Codex app-server that predates a CLI upgrade keeps writing an older catalog
  (README "Lifecycle", step 2).
* A batch that pins `runner_version` 3.0.0 must pin 3.1.0 to launch on this release.
* Editing `registry/` changes every configuration fingerprint, so a paused batch adopts the
  release with `recover repin-config`.
* A project may replace a command prefix such as `/usr/bin/env -u NAME` with
  `"unset_environment": ["NAME"]`; a batch may add `check_overrides`.

**Canary tier:** canary batch. The release changes a model, prompts and the engine: stop the
next batch after its first issue, whose review then uses `gpt-6.1-sol`.

## v3.0.0 — 2026-10-16

Runner housekeeping after a two-agent audit of 2.8.0 (`W-489`: `W-490` to `W-497`) and two
fixes for problems the s212-impl-a-20261015 batch met (`W-506`, `W-507`). The major version is
for what is removed: the `clear-stop` command, the `run --resume` and `measure
--replay-compact` options and the `"full"` value of `intake_mode`. The tracked tree is about
1,100 lines smaller, mostly duplicated definitions, code kept for finished migrations and
verbose registry and schema files.

**Project impact:** a project file that sets `intake_mode: "full"` fails `validate-config` with
`project <name>.intake_mode: must be one of ['compact']`; remove the key or set `"compact"`. No
other configuration key changes. Operators and scripts use `launch --clear-stop` in place of
`clear-stop`, and record a recovery (`recover resume`) and launch in place of `run --resume`.

**Canary tier:** canary batch; the engine's intake, contract check, publication write
(`W-506`) and check-evidence reuse at the last issue (`W-507`) change.

### Changed

* The progress board and the terminal trajectory report take a batch's run directories from
  one function (`records.claimed_runs`: history, then parked, then active), instead of two
  copies that had to stay in step for the board's figures to match the report. Each keeps its
  own reading: the report reads every directory, the board the last one of each issue. The
  terminal report's renderer takes only the usage summary the engine builds; the mapping of
  named usage groups it also accepted had no caller. Both outputs are unchanged: the board
  record of the test fixture and the terminal report HTML are byte-identical. Project impact:
  none. Canary tier: first-issue checkpoint (reporting only) (`W-492`).
* Rules that existed in several copies have one definition each: when a PID counts as alive
  (`config.pid_alive`, before two functions and four inline checks), the SHA-256 of a file
  (`config.file_sha256`, before three functions), the last usage counter of an invocation
  (`records.last_counter`, which `counter_of` and the engine's attempt usage both call, with
  `USAGE_KEYS` declared once), what passing and intact check evidence is
  (`delivery.evidence_intact`, which delivery integrity now uses too), the built-in variable
  names, the backend start timeout default (`LAUNCHER_DEFAULTS`, with the same fallback) and the
  bound marks of the usage tables. `insert-issues` and `sync-linear-template` share the `--home`
  option of the other commands. Figures, comments and messages are unchanged. Project impact:
  none. Canary tier: canary batch (the engine's attempt usage and evidence checks call the
  shared definitions) (`W-493`).
* Tests only: six tests of pure functions no longer build the engine fixture (a Git repository,
  a configuration home, a loaded configuration and a Runner) that they did not use; the Git
  repository setup that six test modules repeated comes from `tests.fixtures.make_repo`; and
  the systemd backend fake that launches succeed with is one method of the supervisor test
  harness instead of two copies. The same 480 tests run with the same assertions. Project
  impact: none. Canary tier: none needed (no runtime change) (`W-494`).
* Documentation describes the runner as it is. README "Layout" is the one module map: it now
  lists `engine/lint.py`, `engine/parity.py` and the `watch` and `insert-issues` commands, and
  AGENTS.md keeps the rules that come with the layout and points to it. AGENTS.md states the
  exception the runner already had: its own usage tables in the run-summary and batch-finished
  comments. README sentences that told release history or incidents ("before 2.6.1", "until
  W-444", the figures of past batches) state current behaviour instead; the history stays in
  this file. The pool table, a copy of `registry/pools.json` that still called the Claude
  canary pending, gives way to a pointer to that file. The recovery and launcher module
  docstrings keep their invariants and point to the README for procedures, which drops two
  stale statements (append-only queue changes, `blocks` in the contract). Project impact:
  none. Canary tier: first-issue checkpoint (documentation only) (`W-495`).
* `registry/pools.json` has one pool entry per line, and the batch, site, workspace and
  registry schemas use the compact style of the project schema; `batch.schema.json` defines the
  model selection and the per-issue phase limit once (`$defs`) instead of six and three times.
  The eleven files shrink from 1,498 to 641 lines. Every file parses to the same JSON (the
  batch schema once its two references are expanded), and validation and its messages are
  unchanged. The pool notes no longer call the Claude canary pending or restate the pools in
  prose. Project impact: none; the parsed policy without its notes is unchanged, so only the
  new runner commit changes a batch's fingerprint, as any commit does. Canary tier:
  first-issue checkpoint (formatting and notes only) (`W-496`).
* The progress board shows times in the local time zone of the runtime it runs in, instead of
  a time zone named in its code. On a host in New York the board shows the same times as
  before. Its test formats the expected time in the same local zone, so it no longer assumes
  New York. Project impact: none. Canary tier: none needed (the board mod only) (`W-497`).

### Removed

* The full intake packet and the code kept for contracts pinned before 2.4.0. Compact intake
  has been the default since `W-181` (2026-09-23): `intake_mode` now accepts only `"compact"`,
  so a project that still states `"full"` fails `validate-config` with `project <name>.intake_mode:
  must be one of ['compact']`; remove the key or set `"compact"`. `measure --replay-compact`, which
  rebuilt saved schema-1 intakes with the compact builder to estimate the saving, goes with it,
  and the measurement drops its `compact_replay` figures and column; `measure` and `report` still
  read schema-1 intakes in saved records. A pinned contract must now match its intake snapshot
  under the current formula; the three earlier formulas (before `W-194`, the intermediate `W-194`
  form, and runners 2.1.0 to 2.3.0 with `blocks`) are gone, and an active issue without
  `repair_blocked` counts its last repair as interrupted instead of reading the latest stop. A
  read-only scan of all saved batch states on 2026-10-15 found none that needs them. Project
  impact: a project that sets `intake_mode: "full"` must remove the key or set `"compact"`; no
  private project sets it. `validate-config` no longer prints `intake_mode`. Canary tier: canary
  batch (the engine builds the intake and checks the contract) (`W-491`).
* `runner.py clear-stop` and `runner.py run --resume`. Both continued a batch without a
  record: `clear-stop` deleted the STOP marker that `launch --clear-stop` clears and records in
  the launch record, and `run --resume` continued a paused issue in the calling process without
  the recorded recovery that `recover resume` writes. Use `launch --clear-stop`, or record a
  recovery (`recover resume` or another kind) and launch. A bare `run` on a batch with a saved
  or paused issue now names those commands in its refusal. `run --max-issues` stays. Also
  removed are names nothing read (`Runner.model_backend`, `Runner.check_key`,
  `attention.MECHANISMS`, `attention.NOTIFIER_BACKENDS`, `recovery.KINDS` and the `__all__`
  of `recovery`) and the `url` setting of the Linear client, which no workspace file could
  supply because the workspace schema has no such key. Project impact: none for configuration;
  an operator or script that called either path uses its replacement. Canary tier: canary
  batch (the engine changes, though only by two deleted methods and two reworded refusals)
  (`W-490`).

### Fixed

* Publication writes the ticked description with every issue mention as its plain identifier
  (`updates.plain_mentions`). It wrote the intake snapshot, which holds Linear's mention markup
  (`<issue id="…" href="…">W-394</issue>`) as read at intake, and when Linear re-saved its own
  markup it moved a backtick of a code span in the same paragraph. In s212-impl-a-20261015 the
  read-back then failed with "Published checklist read-back mismatch" on W-483 (four lines)
  and W-484 (one line), and each needed the description rewritten by hand and `recover
  publish`. Linear links the plain identifiers again, and the read-back comparison, which
  already equates the markup with its identifier, is unchanged. The one other place that sent
  text read from Linear with that markup was a runner comment quoting a pinned criterion (or a
  model's copy of it): `updates.plain` turned the URL inside the markup into inline code and
  left the tags. It now shows the identifier, as inline code like every quoted mention.
  The engine's other writes send states and labels only. Project impact: none. Canary tier:
  canary batch (the engine's publication write changes) (`W-506`).
* At the batch's last issue an extended check reuses evidence only for the same worktree: its
  key also covers the content of every tracked and untracked, non-ignored file, and its records
  say `reuse_scope: "worktree"`. The key covered only the files its `inputs` match, and
  `inputs` also select an extended check at the other issues, so a check scoped by module or
  by environment omits files it runs. In s212-impl-a-20261015 the last issue, W-486, was
  validated after its second repair with the `pytest-slow-synthetic` log of W-485 (29 tests,
  where the reviewed commit has 34) and a `pytest-ci-environment` log from before the repair,
  since the repair changed only test files outside both checks' inputs; the review blocked,
  and the operator ran both commands at the reviewed commit by hand. Default checks and
  extended checks at the other issues are keyed on their `inputs` as before. The last issue
  now pays for it: a repair there reruns every extended check that runs there, and evidence
  from an earlier issue of the batch is not reused there. A per-check reuse list apart from
  `inputs` was not taken, since every project would have to name each file a check runs and a
  missing one would bring the defect back. Project impact: none; no configuration key changes.
  Canary tier: canary batch (the engine's check evidence keys change) (`W-507`).

## v2.8.0 — 2026-10-14

A progress board for batches: a compact `status --board` record, optional lineage keys in a
batch file, and the board itself shipped as a Claude Code mod in the checkout (`W-442`,
`W-443`, `W-444`).

**Project impact:** none. The lineage keys are optional and outside the fingerprint; existing
batch files load as before with the same fingerprint.

**Canary tier:** canary batch; the engine writes one more key (`summary`) into an accepted
issue's `history` entry, and `dry-run` and the launch preflight write `titles.json` into the
state directory.

### Added

* A batch file may state its purpose and the batch it follows, in four optional keys:
  `purpose` (one sentence), `follows` (the `id` of the batch it stems from, or `null` for the
  first of a line of work), `reason` (why it follows) and `from_issues` (the issues whose
  result led to it). §2.10 ran as fifteen batches in six days, and the reason for each existed
  only as prose in its guidance file; a session copied them by hand into a file the runner
  does not know, and for four batches had to infer the predecessor. The keys are a record for
  people that the operator writes with the batch: the engine never reads them, and they are
  outside the configuration fingerprint, so correcting a sentence on a pinned batch needs no
  `recover repin-config`. `validate-config` prints them under `lineage` and `status --json`
  under `lineage` as the batch file holds them now. A `follows` that names no batch file of
  the home is one line under `warnings` of `validate-config`, which still passes. Existing
  batch files are not migrated. Project impact: none; the keys are optional, and a batch file
  without them loads as before with the same fingerprint. Canary tier: first-issue checkpoint
  (the change touches only configuration loading, `validate-config` and `status`) (`W-443`).
* `status --board` prints one compact JSON record of a batch for a progress board, and writes
  nothing. The board built on 2026-10-10 and 2026-10-11 read `status --json`, about 70 KB for
  one batch with the full text of every issue and every event, every ten seconds, and had
  `report` write into a cache folder of its own for time and tokens. The record holds the
  batch (identifier, phase, the supervisor's status and outcome, the time of the first and of
  the last launch, the STOP marker, a pending recovery, the error, and the purpose and
  predecessor of the batch file) and, for each issue in queue order, its identifier, title,
  link, stage, repairs, how often the worker and the review blocked it, the result summary,
  the time of acceptance and its commit, the names of the files for the owner, and the elapsed
  time and token totals with their bounds. Time and tokens are the trajectory report's own
  counting, so `report --runs` on the same run directories gives the same figures, and an
  invocation that has not finished is left out of both. It makes no Linear call. `status` and
  `status --json` print what they printed before. README "Running a batch" lists the fields
  (`W-442`).

* The progress board ships with the runner as a Claude Code mod in `mods/runner-board/`, with
  its plugin manifest and its tests, and the checkout is a plugin marketplace that lists it
  (`.claude-plugin/marketplace.json`). The board was built on 2026-10-10 and 2026-10-11 in the
  operator's own plugin folder: its source was in no repository, it named one host's Python
  and runner paths in its code, and it read `status --json`, `report` and a lineage file of its
  own, so a runner release could break it without a test noticing. It now reads only
  `status --board` and draws the batch file's `purpose`, `follows`, `reason` and `from_issues`;
  the cache folder is gone, and the lineage file is still read for a batch file that has none
  of the keys. The Python executable, the runner's entry point and the private home are
  options of the plugin whose defaults name no host (`python3`, the checkout the plugin is in,
  `~/.config/linear-runner`). One test of the mod draws the `status --board` record of the
  runner's own test fixture, which `tests/reporting/test_board.py` keeps current as
  `testdata/board/status-board.json` and as a module the mod can import. Three lines of an
  opened issue are gone with the full state: the first paragraph of its description, the
  issues it waits for and the text of its last block; a done issue now shows its result
  summary. Worker and reviewer sessions are unchanged and load no plugin. README "Progress
  board" says what the board shows, how to install it from the checkout, that it needs Claude
  Code 2.1.295 or later, and how its tests run (`claude plugin test`; the unit suite does not
  need Claude Code). Project impact: none. Canary tier: first-issue checkpoint (no engine,
  prompt, backend or command changes; the mod runs in the operator's own session) (`W-444`).

### Changed

* The entry the engine appends to `history` when an issue is accepted keeps the summary of the
  accepted result (`summary`). Until now the state held a result summary for the active issue
  only, so nothing said what a finished issue did. A state written before this change has no
  such key, and `status --board` then gives `null` for it; nothing else reads the key.
  `dry-run` and the launch preflight write `titles.json` into the state directory, with the
  title and link of every allowlisted issue: the state holds an issue only once the batch has
  claimed it, so a queued issue had no title. `dry-run` reads each allowlisted issue once more
  for it and still writes no `state.json`. Project impact: none. Canary tier: canary batch
  (the engine writes one more key into `history`, and the launch preflight one more file)
  (`W-442`).

## v2.7.2 — 2026-10-13

One number of the registry changes, so that an issue that processes a whole sample can run as
one phase (`W-464`).

**Project impact:** none. No batch gets a longer phase unless its file asks for one in
`phase_overrides`; a paused batch adopts the new registry with `recover repin-config`.

**Canary tier:** first-issue checkpoint; the change touches only the value that
`validate-config` and the launch preflight accept for a phase timeout.

### Changed

* The largest phase timeout a batch may set for a named issue (`phases.max_timeout_seconds`
  of `registry/phases.json`, used by a batch's `phase_overrides`) is 43,200 s, 12 hours. It
  was 14,400 s. A real example that processes every field of view of a sample in one issue
  was planned at 6.7 to 7.9 hours, which no single phase could hold. The phases' own
  timeouts, the check timeouts and every budget are unchanged, and no batch gets a longer
  phase unless its file asks for one. Project impact: none; a paused batch adopts the new
  registry with `recover repin-config`. Canary tier: first-issue checkpoint (the change
  touches only what `validate-config` and preflight accept) (`W-464`).

## v2.7.1 — 2026-10-11

One sentence of 2.7.0 was wrong, found the same day while its canary was being prepared, before
any batch ran on 2.7.0 (`W-437`).

**Project impact:** none. A batch that pins `runner_version` names 2.7.1; nothing should pin
2.7.0.

**Canary tier:** canary batch; every model prompt changed again, and the canary that 2.7.0
asks for has not run, so one canary on 2.7.1 serves both.

### Fixed

* The sentence about the controller's record in every model prompt, and the README, no longer
  say that the controller rewrites `controller-manifest.json` after every phase. It does not:
  the record is written when the batch pauses on the issue, when the issue is deferred and
  when it is done. The prompt now says so, and that during a later phase the file may still
  name an earlier commit, which is not a defect of the work. Four reviews of the §2.10 batches
  (W-400, W-426, W-428, W-429) listed exactly that as a limitation ("the top-level manifest
  still identifies the earlier commit"); under the sentence of 2.7.0 a reviewer could have
  read it as a fault. The engine is unchanged. Project impact: none. Canary tier: canary
  batch (every model prompt) (`W-437`).

## v2.7.0 — 2026-10-11

The runner housekeeping after the fifteen §2.10 batches (s210-spec-20261006 to
s210-impl-l-20261010, all complete; `W-378`, `W-437` to `W-441`). The batches ran 60 issues
with 93 launches and 83 recorded recoveries. Five of the stops came from the repair output
budget, two from a single Linear request that got no answer, three review rounds from the
controller's `manifest.json`, and the operator wrote wait loops around `watch` and `launch`.
One finding is older: the watchdog of the 2.6.0 canary could not check once the checkout had
changed.

**Project impact:** a project guidance, an acceptance criterion or a `criterion_lint` rule that
names `manifest.json` at the top of the run directory as the controller's file names
`controller-manifest.json`, and a script that reads `<run directory>/manifest.json` reads the
new name. Nothing else changes for site, workspace and project files. A batch that pins
`runner_version` names 2.7.0 before `recover repin-config` adopts the release.

**Canary tier:** canary batch; the engine (the run record, the repair budget, the pin of a
batch that has not run), every model prompt and the Linear client changed.

### Added

* `watch` and `wait` wait for a launch that is still running its checks. `launch` writes
  `launching.json` (its launch id, process and host) into the state directory while it runs;
  while that names a live launch whose supervisor has not reported, `watch` says once "launch
  L-... is running its checks; waiting for its supervisor" and both wait for it. In the §2.10
  batches a `watch` started with the launch of s210-impl-j-20261010 ended with "This batch has
  not been launched", since the checks take about 13 minutes there; beside a relaunch the same
  commands returned the report of the launch before. `launch` now also waits up to 60 s for
  the project lock when the saved state says the batch is paused, as `recover` already does,
  so a relaunch right after a pause needs no loop on `status`. Project impact: none. Canary
  tier: first-issue checkpoint (the launch's own bookkeeping and two read-only commands)
  (`W-438`).

### Changed

* The controller's record of a run is `controller-manifest.json` at the top of the issue's run
  directory (it was `manifest.json`). Every model prompt says that the file is the
  controller's, that a worker never writes it and keeps records of its own under other names,
  and that a reviewer does not count it as the worker's; the generic prompt no longer asks
  for scientific records "in the run manifest". A file at that path that the controller did
  not write is kept as `controller-manifest.replaced-<id>.json`, with a line in the log. In
  s210-impl-c-20261007 the worker of W-382 wrote its own record to `manifest.json` there; the
  controller replaced it without a message and the review found the notes pointing to fields
  that were gone. The criterion the operator then added to every issue caught the
  controller's own file in the review of W-433. Run directories written before the change
  are read by the old name (the batch base, the read-back record). Project impact: a project
  guidance, a criterion or a `criterion_lint` rule that names `manifest.json` at the top of
  the run directory as the controller's file names `controller-manifest.json`; a worker may
  now keep a `manifest.json` there. A script that reads `<run directory>/manifest.json` reads
  the new name. Canary tier: canary batch (the engine and every model prompt changed)
  (`W-437`).

* A pin binds once the batch has state. A batch that was only dry-run (it has
  `resolved-config.json` and no `state.json`) adopts a changed batch file, guidance or runner
  commit at its next `dry-run`, `run` or `launch`: the names are resolved again, the pin is
  replaced and one line says so. While preparing s210-impl-a-20261007 the operator corrected
  one guidance sentence after a dry-run; `dry-run` then refused the change and `recover
  repin-config` refused with "This batch has no pinned state to re-pin", so the sentence had
  to be restored byte for byte. A batch that has state is refused as before and adopts a
  change with `recover repin-config`. Project impact: none. Canary tier: first-issue
  checkpoint (the pin check before a batch's first run) (`W-378`).

* The repair phase's soft output budget is 150,000 tokens (was 50,000) in `registry/phases.json`.
  Five repairs of the fifteen §2.10 batches (s210-spec-20261006, s210-amend-20261006,
  s210-impl-h-20261009) finished ready with 66,138 to 134,258 output tokens, each rewriting
  long specification pages as its findings asked; each stopped its batch at a budget
  checkpoint that the operator reconciled with `recover budget`. These were all the budget
  stops of §2.10. Project impact: none; a paused batch adopts the new registry with `recover
  repin-config`, and a private registry that sets its own repair budget keeps it. Canary tier:
  canary batch (the engine checkpoints a repair at another figure) (`W-440`).

### Fixed

* A read of Linear that gets no answer within the timeout is tried again (after 2 s and 5 s)
  before it stops the batch. On 2026-10-09 the batches s210-impl-h-20261009 and
  s210-impl-i-20261009 each stopped with the class `environment` and "The read operation timed
  out", one minute and fifteen minutes after their launch; nothing was wrong, and `recover
  resume` with another launch cleared both. Their logs name no call: the client let the
  timeout of any single request pass up to the supervisor. Now a `get_` or `list_` tool and the
  opening of the session are sent up to three times, and the stop that remains says "Linear did
  not answer <tool> within 30 s in each of 3 tries". A write is still sent once, since it may
  have been applied, and its stop now names the tool and says so. Project impact: none. Canary
  tier: canary batch (every engine step reads Linear through this path) (`W-439`).

* `watchdog` checks a batch whose configuration files changed after the pin. It works from the
  pinned configuration, as `recover cancel` does, and names the difference in its output under
  `pinned_configuration`; it alerts and stops its own timer as before. Until now such a tick
  ended with "Configuration/guidance changed since ... was pinned" and exit status 2: the
  timer of launch `L-20261006T052322Z-5bf413dc` of canary-w342-20261006 did so five times
  after runner 2.6.1 was committed. That batch was complete, but a running batch whose
  guidance is corrected, or whose runner checkout gets a commit, had no watchdog until it was
  re-pinned. The fifteen §2.10 batches did not meet it. Project impact: none. Canary tier:
  first-issue checkpoint (the watchdog command only; the engine is unchanged) (`W-441`).

## v2.6.1 — 2026-10-06

The two findings of the 2.6.0 canary batch (canary-w342-20261006, complete; `W-351`).

**Project impact:** none. A batch that pins `runner_version` names 2.6.1 before `recover
repin-config` adopts the release.

**Canary tier:** first-issue checkpoint; only the offline `dry-run` selection and the batch
comment's "Files to review" changed.

### Fixed

* `dry-run` selects the next issue with the supervisor's dependency-aware rule: it logs each
  issue that waits for an unfinished prerequisite and the issue it selects, raises nothing
  for a waiting issue and writes no batch state. In the 2.6.0 canary the first issue waited
  for the second and `dry-run` stopped with "Incomplete prerequisite". The batch comment's
  "Files to review" leaves out the worker's outbox drafts (its comments, already in Linear);
  the canary listed one. Project impact: none. Canary tier: first-issue checkpoint (an
  offline selection and a comment) (`W-351`).

## v2.6.0 — 2026-10-06

The findings of the §2.9 amendment batch (s29-amend-20261005, complete; `W-342` to `W-348`).
The batch ran seven issues, one of them added at its checkpoint, and two reviews blocked: one
on a comparison with the base revision that the worker had not kept, one on the literal
reading of "complete suite". The issue added at the checkpoint could be placed before the
last issue only through a Linear relation, which also moved the batch's final extended
checks to the wrong issue, and the operator followed the batch with its own polling loop.

**Project impact:** none for site, workspace and project files; two optional project fields
are new (`criterion_lint`, and `base_parity` on a check). A script that parses `runner.py
status` adds `--json`: `status` now prints a short summary. A batch that pins
`runner_version` names 2.6.0 before `recover repin-config` adopts the release.

**Canary tier:** canary batch; the engine (the last-issue decision, base-parity checks, the
history record), the implement prompt and the batch comment changed.

### Added

* Batch comments list the files a worker made for the owner. At Done the history entry
  records each deliverable with its SHA-256; the next checkpoint or batch-finished comment
  lists, under "Files to review", the deliverables inside each done issue's run directory
  that no earlier batch comment listed; the terminal report lists every deliverable. The
  implement prompt asks the worker to list first the files the issue or the guidance asks the
  owner to look at. In s29-amend-20261005 the W-332 figures for the planned checkpoint were
  only in the Done comment, among ten source files. Project impact: none. Canary tier:
  canary batch (the implement prompt and the batch comment changed) (`W-347`).
* `runner.py watch --batch B [--timeout S] [--interval S]`: prints one line whenever the
  active issue, phase, step, repair count or supervisor status changes, and when the
  supervisor stops prints `wait`'s report and exits with `wait`'s code; `--timeout` ends it
  with code 6 so a caller can start it again. Read-only, like `wait`. The operator session of
  s29-amend-20261005 followed the batch with its own polling loop, re-armed every 30 minutes
  for six hours. Project impact: none. Canary tier: first-issue checkpoint (a new read-only
  command) (`W-346`).
* Project field `criterion_lint`: rules (`pattern`, `message`, optional `unless`) for
  acceptance-criterion wording that reviews read literally. The launch preflight lists each
  match in an issue the batch has not claimed as a warning, and `dry-run` logs it; nothing
  stops. In s29-amend-20261005 "the controller's complete suite" passed two reviews and
  blocked W-338, and "A19 hash lists unchanged" blocked W-336. Project impact: none; a
  project may add rules (the example project has two). Canary tier: first-issue checkpoint
  (preflight output) (`W-345`).
* A check may set `base_parity: {"outputs": [...]}`. The controller then runs its command in
  the issue's starting commit, exported with `git archive` outside the worktree (the
  worktree's path is replaced by the export's in the command and the check environment), and
  in the worktree, and compares the named outputs byte for byte; both runs, both copies of
  the outputs and `parity.json` stay in the validation directory, and a failure names the
  first difference. The base commit is part of the evidence key; the launch baseline skips
  such a check. In s29-amend-20261005 three workers wrote this comparison themselves and the
  W-336 review blocked when the worker had not. Project impact: none; a project adds the
  field to the checks that should compare with the base. Canary tier: canary batch (the
  engine's validation) (`W-344`).
* `recover repin-config --reorder-unclaimed` adopts issues inserted among, and a new order of,
  the issues a stopped batch has not claimed. The allowlist up to its last done, active or
  deferred issue stays as pinned; the option never removes an issue and refuses to move one
  that is started in Linear; the record names the inserted and the moved issues. The new
  offline command `insert-issues` edits the batch file for it (or for `--append-issues`):
  issues inserted before a named issue or at the end, their implement timeout and a new
  `terminal_issue`. In s29-amend-20261005 W-340 had to run before the unclaimed W-333, and
  the operator made W-333 wait for W-340 in Linear and edited the batch JSON by hand. Project
  impact: none. Canary tier: first-issue checkpoint (a recovery option and an offline
  command) (`W-343`).

### Changed

* `status` prints a short summary by default: at most 15 lines with the phase, the active
  issue, its step and repairs, the done, remaining, deferred and waiting issues, the
  supervisor, the STOP marker and a pending recovery; no issue text. `status --json` keeps the
  full output. Before, one call printed every allowlisted issue's description. Project impact:
  none; a script that parses `status` adds `--json`. Canary tier: first-issue checkpoint (an
  operator command) (`W-346`).

### Fixed

* The last-issue rule of the extended checks applies to the issue that runs last, not to the
  last entry of the allowlist. An issue is the last when no other allowlisted issue is still
  to run: done and deferred issues of the batch and issues Done in Linear do not count, an
  issue that waits for it does. In s29-amend-20261005 the appended W-340 was the last entry
  while W-333, which waited for it in Linear, ran after it; the extended checks ran at W-340
  and W-333 got no final run. A Linear read that fails while deciding counts as "last", so
  the final run is never lost to a transient error. Project impact: none. Canary tier:
  canary batch (the engine's check selection) (`W-342`).

### Documentation

* The README says what a later edit of an accepted issue does: nothing stops, because the
  contract is compared only until the read-back right after acceptance; later launches do not
  read it again and the batch end checks only that the issue is still Done. A test pins it. In
  s29-amend-20261005 a wrong number in the accepted W-334 was corrected by a comment because
  this was not written down. Project impact: none. Canary tier: first-issue checkpoint
  (documentation) (`W-348`).

## v2.5.0 — 2026-10-04

The findings of the §2.8 implementation batch (s28-impl-20261002, complete; `W-302` and
`W-303`). The batch took 6 hours 48 minutes for six issues and stopped five times. About 27
minutes went into launch baseline checks on bytes that had just been validated, one review
blocked on two checks that the `when_changed` rule had wrongly skipped, and one pause lasted
19 minutes because nothing told the operator that the batch had stopped.

**Project impact:** none for site, workspace and project files. A batch that pins
`runner_version` must name 2.5.0 in its batch file before `recover repin-config` adopts the
release; the recovery now refuses otherwise, with the launch preflight's message. At the
first launch on 2.5.0 the baseline runs every default check once (saved check evidence has
no `launch_key` yet) and the `model_catalog` step reports a change once. A workspace that
keeps a pasted Linear issue template pastes the new rendering (`sync-linear-template` reports
`differs`).

**Canary tier:** canary batch; the engine (the `when_changed` batch base, the check evidence
keys, the supervisor log) and the repair prompt changed.

### Added

* `runner.py wait --batch B`: blocks until the launched batch's supervisor has stopped and
  prints how. The exit code tells the outcomes apart: `complete` 0, `checkpoint` 3, `paused`
  4 (with the issue, the step, the stop class and the reason), `partial` 5 and `failed` 1. It
  is model-free, reads only the state directory and whether the supervisor's process exists,
  writes nothing and can be started again after every relaunch. s28-impl-20261002 sat paused
  for 19 minutes after the operator session's own polling loop had expired. Project impact:
  none. Canary tier: first-issue checkpoint (a new read-only command) (`W-303`).
* `launch` lists a warning when `attention.notifier.backend` is `none`: stops are then
  visible only in Linear and through `wait`. None of the four unplanned stops of
  s28-impl-20261002 left the runner. Project impact: none; to be notified, configure a
  notifier in the private site or workspace file. Canary tier: first-issue checkpoint
  (launch output) (`W-303`).

### Changed

* The launch preflight's `baseline_checks` step reuses passing check evidence. A default
  check is not run again when `check-cache.json` holds a passing validation or baseline of
  it with the same definition, check environment, launcher environment, executable, identity
  files and matching input bytes; the step's reason names the checks it covered that way. A
  failed, missing or damaged record still runs the check. Four of the six launches of
  s28-impl-20261002 reran the default checks on bytes whose validation had just passed, about
  27 minutes in total. The inherited process environment is left out of this comparison,
  because `launch` and the supervisor's unit never share it; validations compare it as
  before. A baseline check now also runs under its own `timeout_seconds`. Check records gain
  `launch_key`, so evidence saved by an earlier runner is not reused by the first baseline.
  Project impact: none. Canary tier: first-issue checkpoint (preflight) (`W-303`).
* `recover repin-config` refuses when the batch pins a `runner_version` that the checkout is
  not, with the launch preflight's message (both values, and the two ways to resolve it),
  and changes nothing. Before, the re-pin succeeded and only the next launch refused, which
  cost s28-impl-20261002 a second edit and a second re-pin. Project impact: a batch that
  pins `runner_version` must name the new release in its batch file before `repin-config`
  adopts that release. Canary tier: first-issue checkpoint (recovery preflight) (`W-303`).
* The repair prompt that carries review findings (`recover repair`) tells the worker that
  the reviewer's evidence may name examples, not every instance: the worker checks each
  unmet criterion as a whole, fixes every instance it finds and lists in its notes what it
  checked. The first repair of `W-296` fixed the tests its review named; the second review
  found more of the same kind, which used the last repair slot and about 33 more minutes.
  The review prompt, the review result format and the number of repair slots are unchanged.
  Project impact: none. Canary tier: canary batch (prompt) (`W-303`).
* `templates/issue-contract.md` gains a writing rule for "existing tests pass unchanged"
  criteria: the criterion names the edits it allows, or says that a test which pins a layout
  the issue changes may get a named edit that the worker proposes and the reviewer assesses.
  `W-294` stopped on such a criterion, the fifth pause of this kind. Project impact: none for
  configuration; a workspace that keeps a pasted Linear issue template sees `differs` from
  `sync-linear-template` until the new rendering is pasted in. Canary tier: first-issue
  checkpoint (documentation) (`W-303`).
* The supervisor log no longer has the line "input N > limit, but only M was uncached; the
  input budget judges uncached input". Every long phase is in that case, and its nine
  occurrences in s28-impl-20261002 led to no action. `phase-usage.json` still records the
  `budget_note`, and uncached input over the budget still stops the phase with its own
  message. Project impact: none. Canary tier: canary batch (engine, log output only)
  (`W-303`).
* A STOP marker names its reason. `runner.py stop` writes "Stop requested with `runner.py
  stop` at <time>" where it used to leave an empty file, `recover repin-config` writes itself
  into an empty marker, and a launch refusal for a marker that is still empty (the unit's
  `ExecStopPost`) says that it names no reason. Before, a re-pin before a first launch ended
  in "STOP marker present ('')". An existing marker with text is never changed. Project
  impact: none. Canary tier: first-issue checkpoint (launch and recovery messages) (`W-303`).

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
* The `model_catalog` preflight step is reused while the catalog offers the same. Its
  identity was the hash of the catalog file's bytes, and the Codex CLI rewrites `fetched_at`
  and `etag` in `models_cache.json` whenever it refreshes the file, so every launch of
  s28-impl-20261002 reported "ran (changed: model_catalog)". The identity is now the hash of
  the catalog's content without those two keys. The first launch after the upgrade reports
  the step as changed once. Project impact: none. Canary tier: first-issue checkpoint
  (preflight) (`W-303`).

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
