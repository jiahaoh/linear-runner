# linear-runner

A sequential controller for explicitly authorized Linear issue batches. Deterministic
Python owns scheduling, gates, Linear synchronization, checks, the Git commit and
publication. Codex CLI is invoked only for three bounded phases: implementation,
repair and an independent read-only review. No extra Python packages are required.

The engine has no project-specific prompts, owner, workspace or toolchain. Policy lives
in the public `registry/`; everything private (hosts, workspaces, projects, batches)
lives in a private configuration home outside this repository. Your own workflow and
authorization rules belong in project or batch guidance files. One command,
`runner.py launch`, runs a model-free preflight, starts a host supervisor and exits; no
outer model session is needed to start, continue or recover a batch.

Requires Python 3.10+, Git, a POSIX host (tested on Linux), an authenticated Codex CLI and
a Linear credential reachable through an environment variable or the Codex credential
cache. CLI event parsing was established on Codex CLI 0.154.0; probe changed versions
before unattended use.

## Layout

| Path | Contents |
| --- | --- |
| `runner.py` | The engine and CLI (`validate-config`, `dry-run`, `run`, `launch`, `supervise`, `recover`, `status`, `stop`, `clear-stop`, `watchdog`) |
| `launcher.py` | Model-free launch preflight with identity-keyed reuse; `systemd-user` and `foreground` backends |
| `supervisor.py` | The generic supervisor a launched unit runs: scheduling, lifecycle read-back, checkpoints, reporting |
| `recovery.py` | Named, recorded recovery commands and the hash-chained recovery log |
| `rules.py` | One-line decision rules written in issue descriptions |
| `delivery.py` | Generic, config-driven delivery integrity step |
| `config.py` | Layered loading, schema validation, `${variable}` substitution, name resolution pinning |
| `linear_client.py` | Direct HTTPS JSON-RPC client for the official Linear MCP endpoint; append-only comments with read-back |
| `templates/` | Human-review Linear comment templates (DRAFT wording) and the worker/reviewer draft templates |
| `updates.py` | Template rendering, draft lint, hidden event markers and the exactly-once event ledger |
| `messages.py` | Builds each human-review comment from saved state |
| `attention.py` | Stop classification, needs-input mechanisms and the out-of-band notifier |
| `watchdog.py` | Model-free check for a vanished or stalled supervisor (`runner.py watchdog`) |
| `render_samples.py` | Writes `docs/template-samples.md`, one sample comment per template |
| `report.py` | Standalone terminal HTML/JSON report |
| `registry/` | Public policy defaults; each file's `notes` explain its values |
| `schema/` | JSON schemas for every registry file and configuration layer |
| `examples/home/` | Placeholder private home: site, workspace, project and batch files |
| `prompts/` | Generic worker guidance |
| `test_*.py` | Offline tests; `test_public_tree.py` fails on private identifiers in any tracked file |

## Configuration layers

Layers are loaded and merged in this order; later layers never repeat earlier policy.

1. **Registry** — `registry/*.json` in this repository.
2. **Private registry overrides** — optional `<home>/registry/<name>.json`, deep-merged
   onto the matching public file (objects merge, lists and scalars replace).
3. **Site** — `<home>/site.json`: host bindings.
4. **Workspace** — `<home>/workspaces/<slug>.json`: one Linear workspace.
5. **Project** — `<home>/projects/<name>.json`: one repository and Linear project.
6. **Batch** — any file passed with `--batch`: one authorized queue.

The private home is `--home`, else `$LINEAR_RUNNER_HOME`, else `~/.config/linear-runner`.
Every file is validated offline against `schema/`; unknown keys, unknown registry files
and bad references (for example a profile naming an unregistered model or an effort that
model does not allow) are errors.

| Registry file | Holds |
| --- | --- |
| `models.json` | Effort IDs the CLI accepts; models policy may select and their allowed efforts |
| `labels.json` | Task-kind labels and profile labels (exactly one of each per issue) |
| `profiles.json` | Profile → model/effort, profile order, review floors, phase overrides, escalation target, routing version |
| `phases.json` | Per-phase soft budgets and timeouts, the shared repair limit (≤ 2), per-check timeout |
| `linear.json` | Default workflow state names and whether a milestone is required |

| Layer | Fields |
| --- | --- |
| Site | `executables` (must include `codex`), `variables`, `state_root`, `artifact_root`, `model_catalog`, optional `launcher`, optional `attention` |
| Workspace | `slug` (matches the file name), `auth` (exactly one of `token_env` or `credentials_file`, optional `timeout_seconds`), `assignee` (`"me"` or an exact name/email; default `"me"`), optional `states` renames, optional `attention` |
| Project | `workspace`, `linear_project` (exact Linear project name), `repo`, `artifact_owner`, `retention`, optional `backup_status`, `guidance_files`, optional `context_files`, `identity_files`, `check_environment`, `checks`, optional `delivery_checks`, `delivery_integrity` |
| Batch | `id`, `project`, `issues` (ordered allowlist), `terminal_issue`, `branch`, optional `worktree` (defaults to the project `repo`), `guidance_files` (appended after the project's), `required_done`, `human_gates`, `supervision` |

Supervisor, launcher and delivery-integrity fields:

| Layer.field | Key | Default | Meaning |
| --- | --- | --- | --- |
| site `launcher` | `backend` | `systemd-user` | `systemd-user` or `foreground` (in-process; tests and debugging) |
| | `python` | the interpreter running `launch` | Interpreter for the supervisor unit; `${name}` allowed |
| | `cpu_list` | none | Wrap the supervisor in `taskset -c <list>` |
| | `environment` | `{}` | Extra `--setenv` values for the unit, for example `PATH` |
| | `unit_prefix` | `linear-runner` | Unit name is `<prefix>-<batch id>-<launch id>.service` |
| | `startup_timeout_seconds` | 30 | How long `launch` waits to confirm the supervisor started |
| | `stop_on_exit` | `true` | Write the STOP marker when the supervisor exits (also via `ExecStopPost`) |
| batch `supervision` | `stop_after` | `[]` | Planned checkpoints: stop after these issues are accepted |
| | `on_block` | `stop` | Pause the batch on an issue-level block; `continue_independent` opts in to deferring the issue and continuing with independent issues |
| | `report_issues` | `[]` | Extra issues that receive the batch-finished and batch-paused comments |
| | `decision_rules` | `honor` | `ignore` disables rule blocks in issue descriptions |
| | `baseline_checks` | `false` | Run the default-tier checks on the clean baseline during launch preflight |
| workspace `attention` (DRAFT) | `owner_mention` | `""` (comments say "the owner") | Text put where the owner is addressed, for example `@handle` |
| | `needs_input.mechanism` | `mention` | How a stopped issue is marked: `label`, `state` or `mention` (see "Stops") |
| | `needs_input.label` | `Needs input` | Label added on a stop and removed when a recovery runs (must exist in the team) |
| | `needs_input.state` | `Blocked` | Workflow state for the `state` mechanism (must exist in the team) |
| site `attention` (DRAFT) | `notifier.backend` | `none` | `none`, `command` or `linear-mention-only` |
| | `notifier.command` | `[]` | argv run once per stop and watchdog alert; message on stdin, `{subject}` replaced |
| | `notifier.timeout_seconds` | 30 | Notifier command timeout |
| | `watchdog.stall_minutes` | 120 | Watchdog alert after this long without recorded progress |
| | `lint.max_chars` / `max_lines` / `max_first_sentence_chars` | 1500 / 30 / 240 | Limits for worker and reviewer drafts |
| | `outbox.poll_seconds` / `settle_seconds` | 15 / 3 | Outbox poll interval while Codex runs; drafts younger than this are left for the next poll |
| project `delivery_integrity` | `manifest` | required | Renderer manifest, relative to the issue's `delivery/` directory |
| | `revision_field` | required | Manifest field that must equal the committed revision |
| | `required_checks` | `[]` | Check names that must be in the validated evidence |
| | `file_hashes` | `{}` | Manifest field → file (relative to the manifest) whose SHA-256 it must equal |
| | `true_fields` | `[]` | `{"file", "field"}` pairs (relative to the manifest) that must be literally `true` |

The batch state directory is `<state_root>/<batch id>`. Relative paths resolve against the
file that names them. Paths, check arguments, check environment values and credential-file
paths may use `${name}`, where `name` is a site `executables` or `variables` entry or a
built-in: `home` (the private home), `runner_root` (this checkout), `batch` (the batch
`id`) and `worktree` (the batch worktree; not usable in `repo`/`worktree` themselves). An
undefined name is an error. Guidance files may also use `${name}` for host paths; there
only defined names are replaced and any other `${...}` text stays literal. `{run_dir}` (no
dollar sign) in a check argument is still replaced by that check's validation directory at
run time. Commands are argv arrays; no shell is involved.

Credentials are never stored: the workspace names an environment variable or a credential
cache path, and `linear_client.py` re-reads it on each request. Do not put secrets in
`check_environment`; it is recorded in manifests.

### Name resolution and pinning

`validate-config` is fully offline: it creates no state and contacts neither Linear nor
Codex, so the Linear project and assignee remain unresolved names in its report. `dry-run`,
`run` and `launch` resolve the exact project name and the assignee to IDs (no match or more than one
exact match is an error) and write `<state dir>/resolved-config.json` with the resolved IDs,
the effective configuration, the source layer of every value and the layer files used.
Project names must match exactly within the authenticated workspace. Later runs reuse the
pinned IDs without contacting Linear for resolution; a renamed project or assignee, or any
change to the resolved configuration, is refused. Use a new batch `id` for a new queue.

The configuration fingerprint in `state.json` and `resolved-config.json` covers the whole
resolved configuration: guidance text, commands, environment, policy, IDs and the runner
identity. The runner is identified by its Git commit and a clean/dirty flag, not by where
it is checked out: paths under the runner checkout are normalized to `${runner_root}`
before hashing. Moving or re-cloning the runner at the same commit therefore resumes
normally, while a different runner commit, uncommitted runner edits or any configuration
change is refused. The dirty flag is a boolean, so further edits to an already dirty
checkout are not distinguished; run batches from a clean commit. The site `launcher` block
and the site and workspace `attention` blocks are the exceptions: they only choose how the
supervisor process starts and how a person is told about progress and stops, so they are
left out of the fingerprint and changing them never blocks resuming. Each launch record
(`<state dir>/launches/<launch id>.json`) stores the launcher settings actually used, and
`status` shows those of the latest launch.

## Running a batch

1. Read the live issues, dependencies, workflow and repository instructions. Record scope,
   acceptance, data/environment/resource limits and permitted operations in Linear.
2. Prepare a clean dedicated worktree and `codex/` branch from the chosen baseline. Use one
   active controller per project.
3. Copy `examples/home/` to your private home, replace every placeholder and use real
   validation commands. Add project or batch guidance files as needed.
4. Validate offline, run the tests, then launch. Inspect one completed issue before
   continuing with a newly introduced profile or host (a planned checkpoint does this).

```bash
B=~/.config/linear-runner/batches/my-batch.json
python3 runner.py validate-config --batch $B
python3 -m unittest -v
python3 runner.py launch --batch $B                          # preflight, start unit, confirm, exit
python3 runner.py launch --batch $B --stop-after TEAM-123    # same, with a planned checkpoint
python3 runner.py status --batch $B                          # state, supervisor.json, STOP marker
```

`launch` resolves and pins Linear names, runs the preflight below, starts the supervisor
(by default a transient `systemd-run --user` unit with `Restart=no`,
`KillMode=control-group`, logs appended to `<state dir>/supervisor.log` and an
`ExecStopPost` that writes STOP), waits until the supervisor reports itself running and
prints the unit, PID, state directory, log, launch record and terminal-report path. It
then exits; no model or operator session stays attached. A credential named by
`token_env` is passed to the unit by name (`--setenv=NAME`), never by value.
`--backend foreground` runs the supervisor in the launching process instead.

Preflight (`<state dir>/preflight/<launch id>.json`, latest also in `preflight.json`):

| Step | Checks | Reused when unchanged |
| --- | --- | --- |
| `config` | Every layer and the registry validate; resolved fingerprint | configuration |
| `worktree` | Expected branch, not moved outside the controller, clean unless an active issue owns the changes | source (branch, HEAD, clean flag, content hash) + configuration |
| `model_catalog` | Host catalog readable; which registry profiles it offers | catalog bytes + configuration |
| `baseline_checks` (optional) | Default-tier checks pass on the clean baseline | source, configuration, environment (executables, check environment, launcher), fixtures (identity files) |
| `linear` | Authenticated live read of every allowlisted issue, gates, ownership, decision-rule blocks, model/effort per phase, dependency-aware dry-run selection, or the resume checks for a saved active issue | never: live state is always re-read |

Each step records `reused` and a reason (`reused: source, config unchanged since L-...`,
`changed: source`, `no previous preflight result`, `previous result did not pass`,
`live state: always re-read`). `--rerun-preflight` disables reuse. `run --max-issues N`
still works for a single in-process run without the supervisor.

## Lifecycle

For each allowlisted issue, in order. The issue stays in the `in_progress` state through
implementation, checks, repair, commit and delivery, is in the `review` state during
independent review and moves to `done` only after accepted publication. At every step the
live state must be one of that step's own states (by name), so a person moving the issue
elsewhere stops the batch for reconciliation.

1. **Gates.** Every `required_done` issue must be Done. Every human gate pins an issue,
   comment ID, author ID and exact approval text, and its issue must be Done. Gate issues
   cannot be dispatched. Gates are checked before work and again before publication.
2. **Intake.** The live issue must match the resolved project and assignee, have a
   milestone when required, have all `blockedBy` prerequisites Done and be unstarted.
   It needs exactly one task-kind and one profile label. Every phase's model/effort must
   appear in the host CLI `model_catalog` (normally `~/.codex/models_cache.json`);
   an unavailable selection stops before the claim, with no substitution. The catalog
   does not prove remote entitlement or quota.
3. **Implement.** The controller claims the issue (`in_progress` state) and writes an
   intake packet; an interrupted implementation resumes its own session. The worker
   leaves changes uncommitted and makes no Linear or Git mutations.
4. **Checks.** Checks declare `name`, `kind`, `tier`, `inputs`, `cwd` and `command`.
   Default checks always run or reuse evidence; extended checks run for matching changed
   inputs and at the last allowlisted issue. Reuse is keyed on the check definition,
   matching input bytes (including ignored fixtures), the inherited and configured
   environment, the executable and `identity_files`. Only successful records with intact
   log hashes are reused; failures are never reused. Validation that changes source stops.
5. **Repair.** At most `max_repairs` (≤ 2) repairs, shared across resumes and profiles.
   After an unsuccessful repair, one escalation to the registry's `escalation_profile` is
   available. An unchanged failing input stops recovery; an interrupted repair consumes
   its slot.
6. **Commit.** The controller commits the validated, unchanged source itself. Any worker
   commit or other history change stops the batch.
7. **Delivery.** Optional `delivery_checks` receive `RUNNER_DELIVERY_CONTEXT` (a JSON file
   with the revision, validation records, issue, `issue_run_dir` and `delivery_dir`). With
   `delivery_integrity` configured, the controller then verifies, without a model, that
   every recorded check passed with an intact log hash, the required checks are present,
   the renderer manifest exists and names the committed revision, the listed file hashes
   match and the listed flags are `true`; it writes `delivery/integrity.json`. Scientific
   specifics stay in the project's renderer and configuration. A failure stops before review.
8. **Review.** The controller first moves the issue to the `review` state (normally
   In Review) and confirms it by read-back; if the issue is already in that state, for
   example after an interrupted write or a failed review, no second write is made. A
   read-only session then gets a schema bound to the issue ID, the full commit
   and the number of unchecked criteria, and must copy each criterion verbatim. The
   controller independently rejects wrong identities and missing, duplicate, unexpected
   or blank-evidence entries; a summary or schema-shaped output alone is never acceptance.
   The source must still be frozen at the commit afterwards. Review uses at least the
   registry review floor for the issue's task kind and profile.
9. **Publish.** The controller ticks the checklist, sets the `done` state and reads the
   issue back. `[x]` and `[X]` are treated as equivalent (Linear serializes `[X]`); every
   other description byte, identity, ownership, milestone and dependency must match.
   It never closes a human gate or the project.

Each phase records requested and observed model/effort, usage, prompt and tool-output
bytes and elapsed time. Exceeding a phase's soft budget (or missing usage telemetry)
checkpoints the issue for explicit reconciliation (`recover budget`); resume does not
reset it. Usage is the
per-session maximum of cumulative counters summed over sessions; it is not billed cost.

## Linear updates

People read only plain language in Linear; machine records stay in artifacts. Two kinds of
template are kept apart: the agent-review contracts (the structured result and review JSON
schemas) and the human-review templates in `templates/`. Every comment opens with one plain
sentence saying what happened and what, if anything, the owner needs to do, then a few short
optional sections, and at most one `Evidence:` line of host paths. No JSON, code blocks,
tables or long hashes. `python3 render_samples.py` writes one sample of each to
[`docs/template-samples.md`](docs/template-samples.md); the wording is DRAFT.

Every lifecycle event is a NEW comment on the issue it concerns; nothing is edited:

| Event | Author | Posted |
| --- | --- | --- |
| `claim` | runner | when work starts |
| `progress` | worker (outbox draft) | as soon as the runner sees it, while Codex still runs |
| `ready` | worker (outbox draft), runner fallback | when a worker session ends ready |
| `validation` | runner | after each validation run |
| `review` | reviewer (its `summary`), runner fallback | when the review is accepted |
| `done` | runner | after Done is published and read back |
| `blocked` | runner, quoting the worker or reviewer | when the batch pauses (see "Stops") |
| `deferred` | runner | when a rule or `on_block` sets the issue aside |
| `recovery` | runner | when a launch carries out a recorded recovery |
| `batch-finished` / `batch-paused` | runner | on the terminal issue and `report_issues` |
| `watchdog` | model-free watchdog | when the supervisor vanished or stalled |

Each event has the idempotency key (issue, kind, sequence). It is saved in `state.json` as
pending before the write and as posted after the comment is read back. The comment ends with
one hidden line, `<!-- linear-runner <batch>/<issue>/<kind>/<n> -->`. If a write's response
or the following save is lost, the next run finds the comment by that line and adopts it, so
an event is never posted twice. A failed write keeps the batch from advancing; the pending
event is posted when the batch resumes, without rerunning models.

**Outbox.** Codex sessions keep Linear MCP disabled. Each phase attempt has an
`outbox/` directory, and the prompt tells the worker to write drafts there as
`NNN-<kind>.md` (`progress`, `ready` or `blocked`) following `templates/draft-<kind>.md`.
The runner polls the outbox while Codex runs and once after it exits. It lints each draft:
the kind must be allowed for the phase, the first paragraph must be one plain sentence, the
template's required sections must be present and no other headings used, within the length
limits, and with no JSON, code blocks, tables, long hashes or HTML comments except one
optional last `Evidence:` line. Valid `progress` drafts are posted at once. `ready` and
`blocked` drafts wait for the session's result: a ready note is posted when the result is
ready, and a blocked note is quoted in the runner's blocked comment. An invalid draft is
kept on disk, recorded in `state.drafts` and `<attempt>/outbox-lint.json`, and never
posted; the runner posts its own templated fallback where the event needs one. The review
sandbox is read-only, so the reviewer writes its note as the result's `summary`, which the
runner saves as `outbox/NNN-review.md` and lints the same way.

## Supervisor

The unit runs `runner.py supervise --launch-id <id>`, which holds the project lock for its
whole run. It refuses to start (and writes nothing to Linear) unless the preflight for its
launch ID passed against the current configuration and source revision, no STOP marker
exists, no earlier worker is alive, and a paused or unfinished state has a recorded
recovery whose expected state still matches exactly. Then it:

1. completes any missing lifecycle read-back of issues already accepted;
2. finishes the saved active issue, under the recovery's restrictions;
3. selects the first allowlisted issue that is not done or deferred, whose Linear
   `blockedBy` issues are all Done and whose gates pass, and runs it through the lifecycle;
4. after each accepted issue re-validates the accepted result against the pinned
   contract, reads Linear Done and the checked checklist back and writes
   `lifecycle/<issue>/readback.json` (commit, live status, contract hash, SHA-256 of
   `final-result.json`, `intake.json`, `manifest.json` and `delivery/integrity.json`); the
   issue's plain-language `done` comment points to the run directory;
5. stops at a planned checkpoint (`supervision.stop_after` or `--stop-after`), on STOP,
   when nothing more is ready (`partial`, listing deferred and waiting issues) or when the
   queue is complete, writes the terminal report and posts a new `batch-finished` comment on
   the terminal issue and `supervision.report_issues`.

When it carries out a recorded recovery it first posts a `recovery` comment on the issue
and removes the needs-input mark of the stop it recovers.

A batch-level failure (gates, ownership, Linear errors, changed configuration, lost
read-back) pauses the batch as before. An issue-level block (worker or repair blocked,
repair limit, delivery failure, rejected review, soft budget) is recorded in
`state.blocks`; then the first matching decision rule decides, otherwise
`supervision.on_block`: `stop` (the default) pauses the batch, `continue_independent`
(opt-in) defers the issue and continues. Deferral therefore happens only when the batch
opts in or a matching `defer issue when ...` rule asks for it:

| `on_block` | Rule that matches the block | Result |
| --- | --- | --- |
| `stop` (default) | none | batch pauses |
| `stop` (default) | `defer issue when ...` | issue deferred, batch continues with independent issues |
| `stop` (default) | `stop batch when ...` | batch pauses |
| `continue_independent` | none | issue deferred, batch continues with independent issues |
| `continue_independent` | `defer issue when ...` | issue deferred, batch continues with independent issues |
| `continue_independent` | `stop batch when ...` | batch pauses |

Deferring parks uncommitted work in `refs/linear-runner/parked/<batch>/<issue>/<id>`
(plus the manifest's patch/tar snapshot) before restoring a clean worktree; an issue that
already has an unaccepted controller commit is never deferred automatically. Dependents
of a deferred issue keep waiting because their Linear prerequisite is not Done.

## Stops

Every pause is recorded in `state.stops` with a class. The rule is in
`attention.classify_stop` and tested:

| Class | When |
| --- | --- |
| `needs-decision` | worker or review blocked, soft budget exceeded, or something changed outside the runner (issue state, scope, gates, Git history, configuration) |
| `technical-block` | checks still failing after the allowed repairs, delivery failed, or another runner-raised stop |
| `environment` | Linear or Codex errors (authentication, HTTP, timeouts, missing results), OS errors and signals |
| `runner-defect` | any other exception type (a bug in the runner) |

On a pause the runner posts a NEW `blocked` comment on the issue that stopped (the terminal
issue if none was active). Its first sentence says what stopped and addresses the owner
(`attention.owner_mention`); it quotes the worker's or reviewer's own words (a valid
`blocked` or `review` draft, else the result's summary and unmet criteria), says what
decision or action is needed and gives the exact `recover` and `launch` commands. A
`batch-paused` comment goes to the terminal issue and `report_issues` (except the issue that
already got the blocked comment). Then the issue is marked as needing input with the DRAFT
`needs_input.mechanism`:

* `label`: add `needs_input.label` (for example "Needs input") and remove it when a recovery
  runs. The label must already exist in the team.
* `state`: move the issue to `needs_input.state` and move it back to its previous state when
  a recovery runs; launch preflight accepts that state for the saved issue. The state must
  already exist.
* `mention`: change nothing on the issue; the comment's mention is the signal.

Finally the notifier runs once for the stop (`notifier.backend`): `none`, `command` (runs
`notifier.command` with the comment on stdin and `{subject}` replaced by its first sentence,
for example `["mail", "-s", "{subject}", "you@example.org"]` or
`["notify-send", "{subject}"]`; no shell), or `linear-mention-only`. Comments posted with
your own Linear credential may not notify you about your own mention, so an out-of-band
notifier is the dependable signal.

## Watchdog

`runner.py watchdog --batch B` is model-free and meant for a host timer. It alerts once,
with a new comment on the owning issue plus the notifier, when `supervisor.json` still says
`running` but that process is gone (killed, out of memory; no terminal outcome was written),
or when the supervisor runs but nothing in `state.json`, `supervisor.json` or the active run
directory has changed for `watchdog.stall_minutes` (DRAFT 120). Alerts are recorded in
`<state dir>/watchdog.json`, so the same condition never alerts twice; a new stall after
progress alerts again. It never takes the project lock or writes `state.json`. For a
vanished supervisor it also applies the needs-input mechanism, cleared by the next recovery.

Example timer (not created by the runner; pass the Linear credential variable by name if the
workspace uses `token_env`):

```bash
systemd-run --user --unit=linear-runner-watchdog-my-batch --on-calendar='*:0/15' \
  --property=WorkingDirectory=/absolute/path/to/linear-runner --setenv=LINEAR_MCP_TOKEN \
  /usr/bin/python3 /absolute/path/to/linear-runner/runner.py watchdog \
  --batch /absolute/path/to/batches/my-batch.json
systemctl --user list-timers 'linear-runner-watchdog-*'    # inspect
systemctl --user stop linear-runner-watchdog-my-batch.timer # remove after the batch
```

## Stop, recovery and continuation

```bash
B=<batch file>
python3 runner.py status --batch $B      # phase, active issue/step, blocks, deferred, pending recovery, supervisor.json, STOP
python3 runner.py stop --batch $B        # stop between issues (durable STOP marker)
systemctl --user stop <unit>             # immediate: child process group terminated, state preserved, blocked report
```

The supervisor writes a STOP marker whenever it exits (and systemd's `ExecStopPost`
writes one if the unit is killed), so nothing restarts by itself. What the next launch
needs depends on whether a recovery is pending:

* **Bare continuation** (after a planned checkpoint, `stop`, a `partial` outcome or a
  completed queue; nothing is paused and no recovery is pending): inspect `status`, then
  clear the marker explicitly:

  ```bash
  python3 runner.py launch --batch $B --clear-stop [--stop-after ISSUE]
  ```

* **After a recovery**: record it, then launch without `--clear-stop`:

  ```bash
  python3 runner.py recover <kind> --batch $B ... --reason R --authorized-by A
  python3 runner.py launch --batch $B
  ```

  The recovery records the SHA-256 of the STOP marker present when it was recorded, and
  the launch that carries it out removes exactly that marker (the launch record says
  `cleared by pending recovery R-...`). If STOP was absent then, or has changed since (for
  example an operator ran `stop` after recording the recovery), the launch refuses until
  `--clear-stop` is given. Clearing STOP alone never resumes a paused batch or an
  unfinished issue; those always need a recorded recovery.

Every recovery requires `--reason` and `--authorized-by`, runs offline except
`--repin-contract`, and is written to `state.json` and the append-only, hash-chained
`<state dir>/recovery-log.jsonl`. None accepts work or resets history, usage, repairs or
escalation. `--then continue` (the default) lets the supervisor continue the batch after
the recovered issue finishes, just as a fresh launch would; `--then stop` stops after it
(writing STOP again, so a later bare continuation needs `--clear-stop`).

| Situation | Command (then `launch --batch $B`) |
| --- | --- |
| Resume the active issue from its saved step (for example a blocked worker) | `recover resume --batch $B --reason R --authorized-by A [--note-file F] [--then stop]` |
| Re-run only the independent review of the frozen commit | `recover review --batch $B --reason R --authorized-by A [--redeliver] [--repin-contract] [--note-file F]` |
| Soft-budget checkpoint | `recover budget --batch $B --phase P --input-tokens N --output-tokens N --tool-calls N --reason R --authorized-by A` |
| Publication or its read-back failed after acceptance | `recover publish --batch $B --reason R --authorized-by A` |
| Set an issue aside and continue with the others | `recover defer --batch $B --issue ISSUE [--restore-worktree] [--keep-commit] --reason R --authorized-by A` |
| Restore a deferred, parked issue | `recover resume --batch $B --issue ISSUE --reason R --authorized-by A` |
| Withdraw a recovery that was not launched | `recover cancel --batch $B --reason R --authorized-by A` |
| A lifecycle post failed after acceptance | `recover resume --batch $B --reason R --authorized-by A` |

Details: `--note-file` text is stored under the issue's `operator-notes/` with its hash
and appended to later model prompts; it does not change acceptance criteria.
`--repin-contract` adopts an edited live issue before acceptance only, keeping the
previous intake and issue beside it. `--redeliver` re-runs delivery for the frozen commit
and keeps the old packet as `delivery-superseded-<id>`. `review` allows only the review
model phase and `publish` allows none. `budget` moves the checkpoint into
`budget_reconciliations` and records the new limits as that phase's allowance for this
issue. `defer --restore-worktree` parks uncommitted work in a Git ref; `--keep-commit`
continues on top of an unaccepted controller commit. A parked issue can be restored only
when HEAD has not moved since it was parked, or when it was parked cleanly at `implement`.

Verify no previous child is alive before recovering (recoveries and launch refuse a live
recorded PID). Never delete state or reset the worktree to clear a failure. The lock
protects one state directory on one host only; the transient unit survives logout (with
lingering enabled) but not a reboot, and it never restarts itself.

State written by an earlier runner version has a different configuration fingerprint and
is refused; finish or reconcile it with the runner version that created it rather than
editing state.

## Decision rules

An issue description may pre-authorize what the supervisor does when that issue blocks.
Put one fenced block with the info string `linear-runner-rules` in the description, with
one rule per line:

````markdown
```linear-runner-rules
# defer the viewer issue instead of stopping everything
defer issue when worker blocked 2 times on the same criterion
stop batch when review blocked 1 time
```
````

Grammar:

```text
<action> when <event> <N> time|times [on the same criterion | on "<exact criterion text>"]

action  defer issue | stop batch
event   worker blocked   (implementation or repair returned blocked)
        review blocked   (the independent review was rejected)
N       a whole number, at least 1: "1 time", "2 times", ...
```

* The latest `N` blocks of that event are examined. `on the same criterion` also requires
  them to share at least one unsatisfied criterion; `on "<text>"` requires each of them to
  leave that criterion unsatisfied, and the text must match an unchecked criterion of the
  issue exactly (including case and markup).
* Keywords are case-insensitive and may be separated by any spaces. Blank lines and lines
  starting with `#` are ignored.
* Any other line fails launch preflight, and the issue is not dispatched, with a message
  naming the line, for example
  `linear-runner-rules line 2: 'defer issue when worker blocked 2 time': write '1 time' or '2 times'`.
* Rules are read from the issue as pinned at intake; a later edit needs
  `--repin-contract`. The first rule whose condition matches exactly applies; otherwise
  `supervision.on_block` decides (see the table under "Supervisor"). Under the default
  `on_block: stop`, a `defer issue` rule is how an issue opts in to being set aside; in a
  batch that sets `continue_independent`, a `stop batch` rule keeps an issue that must not
  be skipped from being deferred.
* Each rule has a stable id derived from its normalized text (`rule-<12 hex>`). Every
  application is recorded in `state.rule_applications` and the recovery log with the id,
  the normalized text, the blocks it matched and the matched criteria.
* Rules cannot accept work, drop a criterion or change scope. `supervision.decision_rules:
  "ignore"` disables them for a batch.

Parallel workers, distributed leasing, automatic restart and scientific acceptance are
not implemented. Tests use temporary Git repositories with fake Codex, Linear, notifier,
process and launcher boundaries; they make no network calls, send no mail and never run
`systemd-run`.

## License

MIT License; see [LICENSE](LICENSE).
