# linear-runner

A sequential controller for explicitly authorized Linear issue batches. Deterministic
Python owns scheduling, gates, Linear synchronization, checks, the Git commit and
publication. A model CLI (Codex CLI or Claude Code) is invoked only for three bounded
phases: implementation, repair and an independent read-only review. No extra Python
packages are required.

The engine has no project-specific prompts, owner, workspace or toolchain. Policy lives
in the public `registry/`; everything private (hosts, workspaces, projects, batches)
lives in a private configuration home outside this repository. Your own workflow and
authorization rules belong in project or batch guidance files. One command,
`runner.py launch`, runs a preflight (model-free except one tiny start check per model
backend, reused while the backend is unchanged), starts a host supervisor and exits; no
outer model session is needed to start, continue or recover a batch.

Requires Python 3.10+, Git, a POSIX host (tested on Linux), an authenticated Codex CLI (and,
for Claude-backed pools, Claude Code with a claude.ai subscription: its login or a long-lived
token, see "Claude authentication") and a Linear
credential reachable through an environment variable or the Codex credential cache. CLI event
parsing was established on Codex CLI 0.154.0 and Claude Code 2.1.281; probe changed versions
before unattended use.

## Layout

The code is one Python package, `linear_runner/`, with no dependencies outside the standard
library. `runner.py` at the checkout root is the only entry point you run; it forwards to
`linear_runner/cli.py`. Data the code reads (`registry/`, `schema/`, `templates/`) stays at
the checkout root, which is also `${runner_root}`.

| Path | Contents |
| --- | --- |
| `runner.py` | Entry point for every command (`validate-config`, `dry-run`, `run`, `launch`, `supervise`, `recover`, `status`, `stop`, `clear-stop`, `watchdog`, and the offline `report` and `measure`); launch units and comment commands run it by path |
| `render_samples.py` | Entry point that writes (or `--check`s) `docs/template-samples.md` |
| `linear_runner/cli.py` | The command line: argument parsing and dispatch of every command |
| `linear_runner/config.py` | Layered loading, schema validation, `${variable}` substitution, name resolution pinning, the configuration fingerprint and runner identity |
| `linear_runner/engine/runner.py` | The per-issue state machine: gates, model phases, checks and validation, the controller commit, delivery, review, publication and terminal reporting |
| `linear_runner/engine/delivery.py` | Check outcomes and the generic, config-driven delivery integrity step |
| `linear_runner/engine/intake.py` | Worker intake packets: compact schema 2 (default) and the full schema 1 |
| `linear_runner/backends/__init__.py` | The model-backend interface (`SessionRequest`, `Backend`) and the backend registry |
| `linear_runner/backends/codex.py` | Everything Codex-specific: the `codex exec` argv, its JSONL events, execution evidence and the host model catalog |
| `linear_runner/backends/claude.py` | Everything Claude Code-specific: the `claude -p` argv and isolation flags, its stream-json events, execution evidence and the probe-verified model list |
| `linear_runner/linear/client.py` | Direct HTTPS JSON-RPC client for the official Linear MCP endpoint; append-only comments with read-back |
| `linear_runner/linear/updates.py` | Template rendering, draft lint, hidden event markers and the exactly-once event ledger |
| `linear_runner/linear/messages.py` | Builds each human-review comment from saved state |
| `linear_runner/linear/attention.py` | Stop classification, needs-input mechanisms and the out-of-band notifier |
| `linear_runner/supervision/launcher.py` | Launch preflight with identity-keyed reuse; `systemd-user` and `foreground` host backends |
| `linear_runner/supervision/backend_start.py` | The `backend_start.<backend>` preflight step: start each selectable model backend once in the batch worktree |
| `linear_runner/supervision/supervisor.py` | The generic supervisor a launched unit runs: scheduling, lifecycle read-back, checkpoints, reporting |
| `linear_runner/supervision/recovery.py` | Named, recorded recovery commands and the hash-chained recovery log |
| `linear_runner/supervision/watchdog.py` | Model-free check for a vanished or stalled supervisor (`runner.py watchdog`, run by a launch-started timer) |
| `linear_runner/supervision/rules.py` | One-line decision rules written in issue descriptions |
| `linear_runner/reporting/records.py` | Reads saved session, check and intake records from evidence roots and deduplicates copies |
| `linear_runner/reporting/trajectory.py` | Deterministic trajectory/usage/attempts/validation-audit/batch-comparison report (`runner.py report`, and `terminal-trajectory.*` at the terminal step) |
| `linear_runner/reporting/measure.py` | Offline context-cost measurement: intake components, prompt/tool-output bytes, per-call context growth (`runner.py measure`) |
| `linear_runner/reporting/report.py` | Standalone terminal HTML/JSON report |
| `linear_runner/reporting/render_samples.py` | Builds one sample comment per template for `docs/template-samples.md` |
| `templates/` | Human-review Linear comment templates and the worker/reviewer draft templates |
| `registry/` | Public policy defaults; each file's `notes` explain its values |
| `schema/` | JSON schemas for every registry file and configuration layer |
| `examples/home/` | Placeholder private home: site, workspace, project and batch files |
| `prompts/` | Generic worker guidance |
| `testdata/` | Fictional runner records for the renderer and measurement tests |
| `tests/` | Offline tests, laid out like the package (`tests/engine/`, `tests/backends/`, ...); shared fixtures in `tests/fixtures.py`; `tests/test_public_tree.py` fails on private identifiers in any tracked file |

Run the tests from the checkout root; they need no network, Linear, model CLI or systemd:

```bash
python3 -m unittest -v                          # everything
python3 -m unittest tests.engine.test_runner    # one module (or tests.engine.test_runner.EngineTests)
python3 render_samples.py --check               # docs/template-samples.md is current
```

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
| `models.json` | Effort IDs the CLIs accept; models policy may select, the backend that runs each and their allowed efforts |
| `labels.json` | Task-kind labels and profile labels (exactly one of each per issue) |
| `profiles.json` | Profile order, review floors, phase overrides, escalation target, routing version, the `review_routing` low-risk rule |
| `pools.json` | Ordered `{backend, model, effort}` pools per task kind (or `*`), profile and phase (see "Model backends and pools") |
| `phases.json` | Per-phase soft budgets and timeouts, the shared repair limit (≤ 2), per-check timeout, `bounded_sessions` thresholds |
| `linear.json` | Default workflow state names and whether a milestone is required |

| Layer | Fields |
| --- | --- |
| Site | `executables` (must include `codex`, and `claude` when a pool uses the Claude backend), `variables`, `state_root`, `artifact_root`, `model_catalog`, optional `claude` (`auth`: exactly one of `oauth_token_file` or `oauth_token_env`; see "Claude authentication"), optional `launcher`, optional `attention` |
| Workspace | `slug` (matches the file name), `auth` (exactly one of `token_env` or `credentials_file`, optional `timeout_seconds`), `assignee` (`"me"` or an exact name/email; default `"me"`), optional `states` renames, optional `attention` |
| Project | `workspace`, `linear_project` (exact Linear project name), `repo`, `artifact_owner`, `retention`, optional `backup_status`, `guidance_files`, optional `context_files`, `contract_file`, `intake_mode` (`compact` default, or `full`), `identity_files`, `check_environment`, `checks` (each: `name`, `kind`, `tier`, `inputs`, `cwd`, `command`, optional `allow_empty`), optional `delivery_checks`, `delivery_integrity` |
| Batch | `id`, `project`, `issues` (ordered allowlist), `terminal_issue`, `branch`, optional `worktree` (defaults to the project `repo`), `guidance_files` (appended after the project's), `required_done`, `human_gates`, `supervision`, `context_controls`, `model_overrides` |

Supervisor, launcher and delivery-integrity fields:

| Layer.field | Key | Default | Meaning |
| --- | --- | --- | --- |
| site `launcher` | `backend` | `systemd-user` | `systemd-user` or `foreground` (in-process; tests and debugging) |
| | `python` | the interpreter running `launch` | Interpreter for the supervisor unit; `${name}` allowed |
| | `cpu_list` | none | Wrap the supervisor in `taskset -c <list>` |
| | `environment` | `{}` | Extra `--setenv` values for the unit, for example `PATH` |
| | `unit_prefix` | `linear-runner` | Unit name is `<prefix>-<batch id>-<launch id>.service` |
| | `startup_timeout_seconds` | 30 | How long `launch` waits to confirm the supervisor started |
| | `backend_start_timeout_seconds` | 180 | Timeout of each preflight backend start check (`backend_start.<backend>`) |
| | `stop_on_exit` | `true` | Write the STOP marker when the supervisor exits (also via `ExecStopPost`) |
| batch `supervision` | `stop_after` | `[]` | Planned checkpoints: stop after these issues are accepted |
| | `on_block` | `stop` | Pause the batch on an issue-level block; `continue_independent` opts in to deferring the issue and continuing with independent issues |
| | `report_issues` | `[]` | Extra issues that receive the batch-finished and batch-paused comments |
| | `decision_rules` | `honor` | `ignore` disables rule blocks in issue descriptions |
| | `baseline_checks` | `false` | Run the default-tier checks on the clean baseline during launch preflight |
| workspace `attention` | `owner_mention` | `""` | Optional text (for example `@handle`) put as its own paragraph under the first sentence of comments that need action |
| | `needs_input.mechanism` | `mention` | How a stopped issue is marked: `label`, `state` or `mention` (see "Stops") |
| | `needs_input.label` | `Needs input` | Label added on a stop and removed when a recovery runs (must exist in the team) |
| | `needs_input.state` | `Blocked` | Workflow state for the `state` mechanism (must exist in the team) |
| site `attention` | `command_prefix` | `python3 ${runner_root}/runner.py` | How commands in comments start; `${name}` allowed |
| | `notifier.backend` | `none` | `none`, `command` or `linear-mention-only` |
| | `notifier.command` | `[]` | argv run once per stop and watchdog alert; message on stdin, `{subject}` replaced |
| | `notifier.timeout_seconds` | 30 | Notifier command timeout |
| | `watchdog.stall_minutes` | 120 | Watchdog alert after this long without recorded progress |
| | `watchdog.interval_minutes` | 10 | How often the launch-started timer runs the watchdog |
| | `lint.max_chars` / `max_lines` / `max_first_sentence_chars` | 1500 / 30 / 240 | Limits for worker and reviewer drafts |
| | `outbox.poll_seconds` / `settle_seconds` | 15 / 3 | Outbox poll interval while the model runs; drafts younger than this are left for the next poll |
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
cache path, and `linear_runner/linear/client.py` re-reads it on each request. Do not put secrets in
`check_environment`; it is recorded in manifests.

### Name resolution and pinning

`validate-config` is fully offline: it creates no state and contacts neither Linear nor
Codex, so the Linear project and assignee remain unresolved names in its report. `dry-run`,
`run` and `launch` resolve the exact project name and the assignee to IDs (no match or more than one
exact match is an error) and write `<state dir>/resolved-config.json` with the resolved IDs,
the effective configuration, the source layer of every value and the layer files used.
Project names must match exactly within the authenticated workspace. Later runs reuse the
pinned IDs without contacting Linear for resolution; a renamed project or assignee, or any
change to the resolved configuration, is refused. A paused or stopped batch can adopt a
changed configuration or runner commit explicitly with `recover repin-config` (see "Stop,
recovery and continuation"). Use a new batch `id` for a new queue.

The configuration fingerprint in `state.json` and `resolved-config.json` covers the whole
resolved configuration: guidance text, commands, environment, policy, IDs and the runner
identity. The runner is identified by its Git commit and a clean/dirty flag, not by where
it is checked out: paths under the runner checkout are normalized to `${runner_root}`
before hashing. Moving or re-cloning the runner at the same commit therefore resumes
normally, while a different runner commit, uncommitted runner edits or any configuration
change is refused. The dirty flag is a boolean, so further edits to an already dirty
checkout are not distinguished; run batches from a clean commit (`recover repin-config`
adopts a new commit for a paused or stopped batch). The site `launcher` block
and the site and workspace `attention` blocks are the exceptions: they only choose how the
supervisor process starts and how a person is told about progress and stops, so they are
left out of the fingerprint and changing them never blocks resuming. Each launch record
(`<state dir>/launches/<launch id>.json`) stores the launcher settings actually used, and
`status` shows those of the latest launch.

## Model backends and pools

Each model phase runs one entry of a **pool**: `registry/pools.json` maps a task kind (or `*`
for every kind), a profile and a phase to an ordered list of `{backend, model, effort}`.
A task-kind pool replaces the `*` pool of the same profile and phase; `*` must cover every
profile and phase. The profile comes from `profiles.json` as before (issue label, phase
override, review floor, low-risk review, escalation).

Every phase (implement, repair, review) has its own pool and default, and the same override
mechanism. `<name>` is `<model>` or `<model>@<effort>`:

* Issue labels `implement-model:<name>`, `repair-model:<name>`, `review-model:<name>`, and the
  shorthand `model:<name>` for implement and repair; a phase label wins over the shorthand.
* The batch's `model_overrides`, batch-wide per phase and per issue, wins over labels (per
  issue before batch-wide): `{"review": "claude-opus-5-5", "issues": {"TEAM-1": {"implement":
  "gpt-6-luna"}}}`.
* The first entry is the default. A named entry must be in that phase's pool (after review
  floors); otherwise preflight fails before the claim. Nothing is substituted, and Claude's
  `--fallback-model` is never used. The same model may implement and review.
* The single escalation uses the Deep pool of the same task kind and phase: it keeps a named
  model the Deep pool has (at the Deep pool's effort), otherwise the Deep default.
* Each `session.json` records `backend`, `selection` (pool, pool index, `model_source`, and
  lower-precedence names in `shadowed_requests`) and `backend_details`.
* A worker session cannot move between CLIs: when repair or escalation selects another
  backend, the next phase starts a fresh session seeded with a handoff.

Settled pools (first entry is the default; Claude runs only when named until the W-191 canary):

| Profile | Implement / repair | Review |
| --- | --- | --- |
| Deep | gpt-6-astra high, claude-opus-5-5 high | gpt-6-astra high, claude-opus-5-5 high |
| Standard (Research, Validation) | gpt-6-astra medium, claude-opus-5-5 medium | gpt-6-astra medium, claude-opus-5-5 medium |
| Standard (Implementation, Maintenance) | gpt-6-luna max, claude-opus-5-5 medium | gpt-6-astra medium, claude-opus-5-5 medium |
| Economy | gpt-6-luna max, claude-opus-5-5 medium | gpt-6-luna max, claude-sonnet-5 medium (low-risk review only) |

Review floors still apply (Research, Validation and Deep issues are reviewed from the Deep
pool). Claude needs `site.executables.claude` when a default entry or a batch override uses it;
an issue label naming Claude on a site without it fails preflight. Adding the executable to a
site changes the configuration fingerprint of its batches, so a paused batch then needs
`recover repin-config`.

| | Codex (`codex exec`) | Claude Code (`claude -p`) |
| --- | --- | --- |
| Auth | Codex login | claude.ai subscription login (never `--bare`), or a long-lived token (see "Claude authentication"); preflight records only the auth mode and `loggedIn`/`authMethod` |
| Read-only review | OS sandbox (`--sandbox read-only`) | Permission rules: `dontAsk`, tools Read/Grep/Glob/Bash, Bash limited to read-only Git commands |
| Worker | `--approve-for-me` | `acceptEdits` in the worktree and the issue run directory; Bash allowed except Git history/branch commands and nested agents |
| Isolation | Linear MCP disabled | no settings files, a per-session `--settings` (hooks and auto-memory off), `--strict-mcp-config` with no servers, a fixed appended system prompt, `CLAUDE*`/`ANTHROPIC*` variables removed |
| Resume | `resume <id>` | `--resume <id>`; fresh sessions get `--session-id` |
| Result | `--output-schema`, `-o` file | `--json-schema`, `structured_output` of the result event |
| Usage | cumulative per session | per call; the runner accumulates it per session. Input includes cache reads and writes; `total_cost_usd` is kept as the CLI's estimate, never billed cost |
| Observed model / effort | when emitted | model from events; effort is never emitted |
| `compact_token_limit` | supported | not supported: a limit on a phase whose pools include Claude is a configuration error |
| Availability check | host catalog (`site.model_catalog`) | probe-verified model list, CLI version (≥ 2.1.280), the configured authentication |

The Claude reviewer's read-only guarantee is Claude Code's permission rules, not an OS
sandbox; the runner's frozen-source check after the review still stops the issue if the
worktree changed. A Claude error result (for example an unavailable model) stops as an
`environment` stop with the CLI's message, never a silent retry.

### Claude authentication

By default Claude sessions use the host's claude.ai subscription login (`claude auth login`,
kept in `~/.claude`). Every Claude Code process on the host shares that login: interactive
sessions, the desktop app and the runner's `claude -p` calls. Its access token is short-lived,
and when it expires each process refreshes it; concurrent refreshes race, and the loser fails
with "Failed to refresh OAuth token: another Claude Code process is refreshing it or exited
mid-refresh". When the loser is a runner session, the batch stops.

Give the runner its own long-lived subscription token instead: it needs no refresh, so it
cannot race. Create it once with `claude setup-token` (interactive; it prints the token) and
keep it in a file only you can read, outside any Git repository:

```bash
claude setup-token                                                  # prints a long-lived token
install -d -m 700 ~/.local/share/linear-runner
(umask 077; cat > ~/.local/share/linear-runner/claude-oauth.token)  # paste the token, Enter, Ctrl-D
chmod 600 ~/.local/share/linear-runner/claude-oauth.token
```

Then reference it in the private `site.json` (the token itself never goes in configuration):

```json
"claude": {"auth": {"oauth_token_file": "~/.local/share/linear-runner/claude-oauth.token"}}
```

or `{"oauth_token_env": "NAME"}` for a variable set in the environment that runs `launch`.

* **Token file.** `validate-config` and launch preflight check that it is a regular, non-empty
  file with mode 600 or stricter, and never print it; a path inside the worktree or this
  checkout is refused. The runner reads it when each Claude session starts and puts it only in
  that `claude` process's environment, as `CLAUDE_CODE_OAUTH_TOKEN`: never in argv,
  `session.json`, `events.jsonl`, `resolved-config.json`, logs, launch records or unit
  properties. The systemd unit gets no secret, and a replaced token is used from the next session.
* **Token variable.** Preflight fails if it is unset. `launch` passes it to the unit by name
  (`--setenv=NAME`, like the Linear `token_env`); the runner removes it from checks and Codex
  sessions and gives it only to `claude`, as `CLAUDE_CODE_OAUTH_TOKEN`.
* **Preflight.** In a token mode the `claude_auth` step (always re-run) checks that the token is
  readable and that `claude auth status` reports `authMethod: oauth_token`; the claude.ai login
  is not required. That command is local; an expired or revoked token shows up at the
  `backend_start.claude` step (a tiny real call with the token, re-run when the token file or
  mode changes) or, if the token is revoked after that check, at the first session.
* **Records.** Preflight and `session.json` record the auth mode (`subscription-login`,
  `oauth-token-file` or `oauth-token-env`), never the token. The configuration fingerprint covers
  the mode and the file path or variable name, not the token, so replacing the token changes
  nothing; adding or changing the setting does, and a paused batch then needs
  `recover repin-config`.
* **Stops.** A Claude authentication error (OAuth, 401, login) is an `environment` stop that
  names the auth mode. Its blocked comment says to regenerate the token with `claude setup-token`
  (token modes), or suggests a token file because concurrent Claude Code sessions can race on
  the refresh (subscription login).

Claude Code decides whether the tools it runs (for example the worker's Bash) see
`CLAUDE_CODE_OAUTH_TOKEN`; treat the token like the login it replaces, which a worker could also
read from `~/.claude`.

## Running a batch

1. Read the live issues, dependencies, workflow and repository instructions. Record scope,
   acceptance, data/environment/resource limits and permitted operations in Linear.
2. Prepare a clean dedicated worktree and `codex/` branch from the chosen baseline. Use one
   active controller per project.
3. Copy `examples/home/` to your private home, replace every placeholder and use real
   validation commands. Add project or batch guidance files as needed.
4. Validate offline, run the tests, then launch. Inspect one completed issue before
   continuing with a newly introduced profile or host (a planned checkpoint does this).

**Canary policy.** After any change to runner behavior, the configuration schema, the model
backend or a model, first run one small canary batch of low-risk real issues, and fix what
it finds before you start a production batch. Unit tests and offline `validate-config` do
not exercise live dispatch.

`--batch` takes the batch file's path or, for a file at `<home>/batches/<id>.json` in the
active private home, just its `id`; an unknown id is an error. Comments show `--batch <id>`
when the id resolves to the batch's own file, and the full path otherwise.

```bash
B=my-batch                                   # or a path: ~/.config/linear-runner/batches/my-batch.json
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
`token_env` (or a Claude `oauth_token_env`) is passed to the unit by name (`--setenv=NAME`),
never by value. Before the
supervisor it starts the watchdog timer (see "Watchdog"); if the timer cannot start, nothing
is launched. `--backend foreground` runs the supervisor in the launching process instead and
starts no timer.

Preflight (`<state dir>/preflight/<launch id>.json`, latest also in `preflight.json`):

| Step | Checks | Reused when unchanged |
| --- | --- | --- |
| `config` | Every layer and the registry validate; resolved fingerprint | configuration |
| `worktree` | Expected branch, not moved outside the controller, clean unless an active issue owns the changes | source (branch, HEAD, clean flag, content hash) + configuration |
| `model_catalog` | Host catalog readable; which registry profiles it offers | catalog bytes + configuration |
| `baseline_checks` (optional) | Default-tier checks pass on the clean baseline | source, configuration, environment (executables, check environment, launcher), fixtures (identity files) |
| `claude_auth` (with `site.claude.auth`) | The Claude token file or variable is usable and the CLI uses it (see "Claude authentication") | never: always re-read |
| `linear` | Authenticated live read of every allowlisted issue, gates, ownership, decision-rule blocks, model/effort per phase, dependency-aware dry-run selection, or the resume checks for a saved active issue | never: live state is always re-read |
| `backend_start.<backend>` | Each model backend the pending issues can select starts in the batch worktree (see below) | backend: executable (path, resolved file, SHA-256), CLI version line, auth mode (Codex `login status`; Claude mode + token file path/variable name + token file mtime), worktree, probe model/effort, launcher and check environment, start-check code |

Each step records `reused` and a reason (`reused: source, config unchanged since L-...`,
`changed: source`, `no previous preflight result`, `previous result did not pass`,
`live state: always re-read`). `--rerun-preflight` disables reuse. `run --max-issues N`
still works for a single in-process run without the supervisor.

**Backend start check.** A backend that cannot start in the worktree (for example a CLI
upgrade that rejects the project layout, or a token the service refuses) would otherwise
fail only after an issue is claimed. For each backend that any pending issue can select (every
phase as routed now, repair and review after the escalation, the lighter review when enabled;
labels and batch `model_overrides` included), `backend_start.<backend>` runs the worker command
once: the backend's own argv and child environment (Codex `exec --approve-for-me --add-dir
<artifact root> -C <worktree> ... --json -o ... --output-schema ... -`; Claude `-p` with the
worker's settings, `acceptEdits` permissions and the configured token), in the worktree, with
the launcher environment the unit gets. Only the request is small: a fixed prompt asking for
`{"ok": true}`, a one-field schema, the backend's first selectable model at the lowest effort
it accepts for it (for example `gpt-6-luna` `low`, `claude-opus-5-5` `low`) and
`site.launcher.backend_start_timeout_seconds` (180). This is a real model call of a few
thousand input tokens (no model-free CLI command exercises the start path), so the result is
reused until the backend identity above changes. A failure stops `launch` before any issue is
claimed, naming the backend and quoting its own error; the prompt, schema, events and stderr
are kept in `<state dir>/preflight/<launch id>/backend_start/<backend>/`. The token never
appears in them.

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
   intake packet (see "Context cost"); an interrupted implementation resumes its own
   session unless bounded sessions start a fresh one from a handoff. The worker
   leaves changes uncommitted and makes no Linear or Git mutations.
4. **Checks.** Checks declare `name`, `kind`, `tier`, `inputs`, `cwd` and `command`.
   Default checks always run or reuse evidence; extended checks run for matching changed
   inputs and at the last allowlisted issue. Reuse is keyed on the check definition,
   matching input bytes (including ignored fixtures), the inherited and configured
   environment, the executable and `identity_files`. Only successful records with intact
   log hashes are reused; failures are never reused. Validation that changes source stops.
   **Empty checks:** a check may set `"allow_empty": true`. Its exit code 5 (pytest's "no
   tests collected", for example when a marker selects nothing after the tests moved) is then
   recorded as `status: "empty"` with the note "no tests selected; not applicable" and counts
   as passing; the validation comment lists it as "<name> selected no tests (allowed)", and
   `report`/`measure` show the outcome as "empty". Any other nonzero exit, and exit 5 without
   the flag, is still a failure. The flag is part of the check definition, so changing it
   changes the configuration fingerprint and the check's reuse identity. Every check record
   in `checks.json` has `status` (`passed`, `failed` or `empty`) and `allow_empty`.
   Each run of the checks writes `validation-<UTC second>-<random>/checks.json` in the issue's
   run directory. These names do not order validations within one second, so the state names
   the validation that counts: `active.validation_dir` while the issue runs and
   `history[].validation_dir` once it is Done.
5. **Repair.** At most `max_repairs` (≤ 2) repairs, shared across resumes and profiles.
   After an unsuccessful repair, one escalation to the registry's `escalation_profile` is
   available. An unchanged failing input stops recovery; an interrupted repair consumes
   its slot. A repair that finishes with status `blocked` also used its slot; the state
   records it (`active.repair_blocked`) so the blocked comment and `recover resume` can tell
   it from an interrupted repair (see "Stop, recovery and continuation").
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
   The source must still be frozen at the commit afterwards, and the shared contract must
   still have its pinned hash. Review uses at least the registry review floor for the
   issue's task kind and profile (the opt-in low-risk rule may lower only the default floor).
9. **Publish.** The controller ticks the checklist, sets the `done` state and reads the
   issue back. `[x]` and `[X]` are treated as equivalent (Linear serializes `[X]`); every
   other description byte, identity, ownership, milestone and dependency must match.
   It never closes a human gate or the project.

**Issue contract.** Intake pins a SHA-256 of the issue's `id`, `description`, `projectId`,
`assigneeId`, `projectMilestone` and the `blocks`, `blockedBy` and `duplicateOf` relations
(`issue_contract`; relations by issue ID only, sorted, so renaming or reordering a blocker or
duplicate is not a change), and keeps the full issue as the intake snapshot (`active.issue`). Every
resume, the launch preflight, publication and the lifecycle read-back compare the live issue
with it; a difference stops the batch. `relatedTo` is not part of it: Linear adds related
links by itself whenever a description or comment (the runner's own comments included)
mentions another issue, and re-creates them from description mentions after removal, so they
say nothing about scope. Title, labels and status are outside the contract too. State pinned
by an earlier runner version stored a hash that included the related links and relation
titles; the runner checks that stored hash against the intake snapshot (under the current or
an earlier formula) and then
compares the snapshot and the live issue with the current field set, so a paused batch
continues without a new batch.

Each phase records requested and observed model/effort, usage, prompt and tool-output
bytes and elapsed time. Exceeding a phase's soft budget (or missing usage telemetry)
checkpoints the issue for explicit reconciliation (`recover budget`); resume does not
reset it. Usage is the
per-session maximum of cumulative counters summed over sessions; it is not billed cost.
The attempt's `phase-usage.json` names its `basis`: `delta` (its cumulative session
counter minus the session's previous counter, exact), `invocation` (per-call counters,
exact), `cumulative-upper-bound` or `unavailable`. When an earlier attempt of the same
session failed before any completed turn and recorded no counter, the resumed attempt's
figure is its cumulative counter minus the last known one: an upper bound that also holds
the failed attempt's unreported usage. The budget check uses it as is, so an upper bound
under budget passes and one over budget checkpoints (the stop says "an upper bound").
Telemetry counts as unavailable only when the attempt itself reports no counter; an
unknown figure stays unknown, never zero.

## Context cost

Batch-3 measurements showed that almost all input tokens are cached re-reads of the session
context: cost follows the number of model calls times the context size, not the number of
invocations. These controls keep the context small. Bounded sessions and the low-risk review
are off unless a batch opts in:

```json
"context_controls": {"bounded_sessions": false, "low_risk_review": false, "compact_token_limit": null}
```

The two switches default to `false` and `compact_token_limit` to `null`; all are schema-validated and and are part of the pinned, fingerprinted batch
configuration (turning one on for a running batch refuses a resume like any other change).
The thresholds and the rule itself stay in the registry.

**Auto-compaction threshold (optional, Codex only).** A registry phase may set `compact_token_limit`
(unset for every phase by default), and a batch may set `context_controls.compact_token_limit`
(an integer of at least 1,000, or `null`) for all phases; the batch value wins. When a value is
set, every fresh and resumed call gets `-c model_auto_compact_token_limit=<N>`, a config key of
Codex CLI 0.154.0 (it is type-checked as an integer; the CLI silently ignores unknown keys, so
re-check it after a CLI upgrade). Each `session.json` records `compact_token_limit` (`null` when
unset), and `measure` and `report` show it per invocation. Claude Code has no such per-call
threshold, so a limit that applies to a phase whose pools include a Claude entry is rejected
when the configuration loads.

**Compact intake and shared contract.** A project may name a `contract_file`: one versioned
file with the rules every issue follows. Its SHA-256 is part of the pinned configuration,
so editing it refuses a resume, and the runner re-checks it before and after review and at
publication. The default `intake_mode: "compact"` writes `intake.json` schema
`linear-runner.intake/2`: the issue's own fields and description, the exact unchecked
criteria, the contract as `{path, sha256, bytes}`, the other guidance inline, context files as
`{path, bytes, sha256}` (read on demand, not inlined), checks, selection and operator notes.
The full pinned Linear issue is written to `issue.json` and remains the identity record for
review and lifecycle read-back. The reviewer is told the exact contract version.
`intake_mode: "full"` keeps the previous packet. `templates/issue-contract.md` is the shape
for writing issue descriptions against the shared contract (purpose, deliverables, 5 to 10
verifiable criteria, optional decision rules, exclusions); `examples/issue-contract-example.md`
is a fictional example and `examples/home/contracts/shared-contract.md` an example contract.
The runner also writes `intake-components.json` (bytes per component) for later measurement.

**Measurement.** `runner.py measure --runs <dir>... [--issues ...] [--rollouts DIR | --no-rollouts]
[--replay-compact] [--json out.json]` reads saved records only. Codex session logs are read from
`--rollouts`, else `$CODEX_HOME/sessions`, else `~/.codex/sessions`; when that directory is
missing, the output says so and omits the per-call growth instead of failing. Claude invocations need no
session logs: their per-call context comes from the invocation's own `events.jsonl`. It reports intake bytes by
component, unchecked criteria and link-markup bytes, and per invocation the prompt and
tool-output bytes and the input it added to its session. With `--rollouts` it reads the Codex
rollout of each session and splits each invocation's input into the re-sent starting context
and the growth charged to what added it (intake/context reads, test runs, file reads, other
commands, turns without tools, compaction); the parts add up to the recorded input exactly.
`--replay-compact` rebuilds each saved intake with the compact builder and reports its size.

**Trajectory report.** `runner.py report --runs <dir>... [--issues ...] [--until <ISO time>]
[--group "LABEL=ID,ID"] [--out DIR] [--check-trajectory recorded.json] [--check-comparison
recorded.json --check-label LABEL]` renders per-issue usage and time, attempts, sessions, a
validation audit (log hashes re-verified) and batch comparison rows as Markdown and HTML plus
JSON, with no model. Copies of run directories are deduplicated by session ID + start + role;
the latest cumulative counter of each session counts once; cached input and reasoning are
subsets; a missing counter is unknown, never zero; an unfinished invocation is listed as pending
and not counted. An attempt that follows an attempt of the same session without a counter (a
failed turn) has only an upper bound for its delta: the attempts table shows it as `≤ N` and
the JSON gives `usage_basis: "cumulative-upper-bound"` (`measure` marks its "input added" the
same way); the session's counter still counts once in the totals. Tool calls are the CLI's
completed tool calls (failed ones are a subset).

**Totals and marks.** The report tables and the runner's run summary comments (see "Linear
updates") compute totals with one function (`trajectory.totals`), so they show the same
figures. A total is the sum of what the attempts reported, and it is exact only when every
attempt in it reported the figure. Otherwise it is shown as `≥ N`: at least N, because an
attempt reported nothing and may have used more. An attempt's own `≤ N` does not make a total
an upper bound: its excess can only be usage that the counterless attempt before it did not
report, and that attempt is part of the same total, so the total never over-counts (a
total is `≤ N` only if an upper-bound part had no counterless attempt beside it). Nothing
known is "unknown" in the report and "—" in comments, never zero. The JSON keeps each
issue's `usage` (null when a session had no counter) and adds `totals`, each figure as
`{value, bound}` with `bound` `exact`, `lower-bound` or `upper-bound`. The `--check-*` options compare against recorded totals field by field. At
every terminal outcome the supervisor also writes `terminal-trajectory.{json,md,html}` next to
the terminal report from the batch's run directories; a rendering failure is logged and never
blocks the terminal report.

**Bounded worker sessions (batch opt-in `context_controls.bounded_sessions`).**
`registry/phases.json` `bounded_sessions`: `input_threshold_tokens` (5,000,000),
`handoff_after_implement` (true), `max_handoff_bytes` (12,000). When a batch opts in, workers are asked to write
`<attempt>/handoff.json` (`templates/handoff.md`, `schema/handoff.schema.json`). A worker session
whose cumulative input reached the threshold, or the first repair after implement, is not
resumed: the next phase starts a fresh session whose prompt carries the handoff. A missing,
oversized, invalid or wrong-issue handoff is replaced by one the runner builds from the last
structured result, the diff and the latest checks. Each switch is recorded in state and in the
new attempt's `session.json` (`handoff`: source, reason, path, hash); repairs, the single
escalation, issue identity, frozen-source checks and per-session usage attribution carry over.

**Low-risk review routing (batch opt-in `context_controls.low_risk_review`).**
`registry/profiles.json` `review_routing.light_review`: when a batch opts in, the review may use `profile` (Economy) when all
of these hold: the issue's profile label is in `issue_profiles` (Economy, Standard) and its task
kind in `task_kinds` (Maintenance, Implementation); it has no `opt_out_labels` ("Full review")
or `gate_labels` ("Human gate") label and is not blocked by a batch human-gate issue; the first
validation passed with at most `max_repairs` (0) repairs and no escalation; delivery checks
passed; and the committed diff has at most `max_changed_files` (8) files and
`max_changed_lines` (300) lines. The Research, Validation and Deep review floors still apply;
the opt-out label or a Deep profile label keeps a high-effort review. The evaluation is saved
to `review-risk.json` and the selection source reads "low-risk review rule".

## Linear updates

People read only plain language in Linear; machine records stay in artifacts. Two kinds of
template are kept apart: the agent-review contracts (the structured result and review JSON
schemas) and the human-review templates in `templates/`. Every comment opens with one plain
sentence saying what happened and what, if anything, the owner needs to do, then a few short
optional sections, and at most one `Evidence:` line of host paths. No JSON, tables or long
hashes. The one exception to "no code blocks": each command the owner may run is in its own
fenced `bash` block, after one plain sentence saying what it does. The one exception to "no
tables": the runner's usage tables (below). Model drafts may use neither. Commands start with
`attention.command_prefix` and use `--batch <id>`. `python3 render_samples.py` writes one
sample of each to [`docs/template-samples.md`](docs/template-samples.md).

**Models in comments.** Every comment about a model stage says which model, effort and
backend ran it (for example "Implemented with gpt-6-luna (max effort, Codex)"): the claim
lists the planned implementation, repair and review models (and names the label or batch
entry that chose a non-default one), worker and reviewer notes carry it in their attribution
line, the runner's ready and review fallbacks, the repair announcement in a failed
validation (including an escalation), the Done summary, blocked comments for a model phase,
and recoveries that start a model phase. `session.json` keeps the full selection record.

**Deliverables.** The worker's readiness result has a `deliverables` list: each file the
owner should review (for example a rendered report) as `{path, description}`, or `[]`. A
path must exist inside the worktree or the issue's run directory; others are dropped and
named in the ready comment. The reviewer is told the listed deliverables. When the project
has `delivery_integrity`, the delivery packet's `file_hashes` files (or the manifest when
there are none) are added. The `done` comment lists them under "Deliverables to review",
one per line, and leaves the section out when there are none.

**Issue mentions.** Linear links a bare issue identifier (`TEAM-123`) or issue URL in a
comment to that issue and adds a "related" link between the two issues. Where a runner
comment quotes someone's words (a recovery reason, an owner note, a worker or reviewer
summary, criteria and evidence, error text), those mentions are shown as inline code
(`` `TEAM-123` ``) by one helper, `updates.neutralize_issue_mentions`. This assumes Linear does
not link text inside a code span, which cannot be tested offline; related links are also not
part of the issue contract (see "Lifecycle"), so a link that does appear no longer stops a
batch. Worker and reviewer drafts are posted unchanged, and the runner's own issue lists (the
batch summaries on the terminal issue) name issues as usual. `recover` prints a warning when
the reason or note names an issue: it may still be written that way, but prefer describing
the other issue when no link at all should appear.

**Usage tables.** The `batch-finished` comment has a table with one row per issue of the
batch (Issue | Outcome | Attempts | Input (cached) | Output | Tool calls | Time) and a bold
batch total row. Its figures are those of `terminal-trajectory.*`: the same saved records
and the same totals as `runner.py report` (see "Totals and marks" under "Context cost").
Token counts are compact (812, 2.1k, 39k, 1.60M), cached input is in parentheses and is part
of the input, times are model plus check time (45 s, 24 min, 1 h 05 min), `≤ N` is an
attempt's upper bound, `≥ N` a total that misses what an attempt did not report, and `—` a
figure that was not recorded (never 0). There is no cost column: the counters are not billed
cost. When the table cannot be rendered the comment says so and points to the report. A
table is allowed only in runner comments: the lint (`updates.lint(..., allow_tables=True)`)
requires a header row, a delimiter row and rows of equal width in their own paragraph,
still rejects JSON, long hashes and HTML comments in cells, and does not count table lines
against `max_chars`/`max_lines`, which bound prose. Model drafts still may not use tables.

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

**Outbox.** Model sessions have no Linear MCP. Each phase attempt has an
`outbox/` directory, and the prompt tells the worker to write drafts there as
`NNN-<kind>.md` (`progress`, `ready` or `blocked`) following `templates/draft-<kind>.md`.
The runner polls the outbox while the model runs and once after it exits. It lints each draft:
the kind must be allowed for the phase, the first paragraph must be one plain sentence, the
template's required sections must be present and no other headings used, within the length
limits, and with no JSON, code blocks, tables, long hashes or HTML comments except one
optional last `Evidence:` line. Valid `progress` drafts are posted at once. `ready` and
`blocked` drafts wait for the session's result: a ready note is posted when the result is
ready, and a blocked note is quoted in the runner's blocked comment. An invalid draft is
kept on disk, recorded in `state.drafts` and `<attempt>/outbox-lint.json`, and never
posted; the runner posts its own templated fallback where the event needs one. The review
sandbox is read-only, so the reviewer writes its note as the result's `summary`, which the
runner saves as `outbox/NNN-review.md` and lints the same way. Every backend's prompt states
these rules in the same words (`updates.draft_rules`), built from the site `lint` limits and
the templates' required sections, including that the first paragraph is exactly one sentence
of at most `max_first_sentence_chars` characters. A real Claude review summary written under
these rules is kept in `tests/backends/claude_samples/review-summary.md` and must keep passing
the lint.

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

**Exit status.** Every orderly outcome exits 0: a planned checkpoint, STOP, a `partial` or
complete queue, and a recorded pause (a classified stop with its blocked comment, including
a `systemctl stop`). The supervisor exits nonzero only when it refuses to start (2), when a
pause is classified `runner-defect` (an unexpected exception type, 1) or when recording the
pause itself fails, so a systemd unit ends `failed` only for real failures. `status`,
`launch` confirmation and the watchdog read `supervisor.json` (`status`, `outcome` and,
for a pause, `stop` and `classification`), never the exit code.

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
| `environment` | Linear or model CLI errors (authentication, HTTP, timeouts, error results, missing results), OS errors and signals |
| `runner-defect` | any other exception type (a bug in the runner) |

On a pause the runner posts a NEW `blocked` comment on the issue that stopped (the terminal
issue if none was active). Its first sentence says what stopped and that it needs your
decision or action (an optional `attention.owner_mention` follows as its own paragraph); it
quotes the worker's or reviewer's own words (a valid
`blocked` or `review` draft, else the result's summary and unmet criteria), says what
decision or action is needed and gives the exact `recover` and `launch` commands. A
`batch-paused` comment goes to the terminal issue and `report_issues` (except the issue that
already got the blocked comment). Then the issue is marked as needing input with
`needs_input.mechanism`:

* `label`: add `needs_input.label` (for example "Needs input") and remove it when a recovery
  for that issue is carried out. The label must already exist in the team. Each write reads
  the issue's labels, writes the full set with the label added or removed, and reads it back,
  so other labels are kept.
* `state`: move the issue to `needs_input.state` and move it back to its previous state when
  a recovery runs; launch preflight accepts that state for the saved issue. The state must
  already exist.
* `mention`: change nothing on the issue; the comment's mention is the signal.

Finally the notifier runs once for the stop (`notifier.backend`): `none`, `command` (runs
`notifier.command` with the comment on stdin and `{subject}` replaced by its first sentence,
for example `["mail", "-s", "{subject}", "you@example.org"]` or
`["notify-send", "{subject}"]`; no shell), or `linear-mention-only`. The default is `none`.
Comments posted with your own Linear credential do not notify you, so without a notifier the
Linear comments, the label and the watchdog are what make a stop visible.

## Watchdog

`runner.py watchdog --batch B` is model-free. It alerts once, with a new comment plus the
notifier, when `supervisor.json` still says `running` but that process is gone (killed, out
of memory; no terminal outcome was written), or when the supervisor runs but nothing in
`state.json`, `supervisor.json` or the active run directory has changed for
`watchdog.stall_minutes` (120). The comment goes to the active issue, or the terminal issue
when none is active, and that issue gets the needs-input mark, removed when the next launch
starts. Alerts are recorded in `<state dir>/watchdog.json`, so the same condition never
alerts twice; a new stall after progress alerts again. It never takes the project lock or
writes `state.json`.

`launch` (systemd backend) starts it automatically: before the supervisor unit it starts a
transient user timer `<prefix>-<batch>-<launch id>-watchdog.timer` (with a matching
`.service`) that runs `runner.py watchdog --batch <id> --launch-id <id> --timer <timer>`
every `watchdog.interval_minutes` (10), logging to `<state dir>/watchdog.log`. The timer is
recorded in the launch record and shown by `status`. It stops:

* by itself, once that launch's supervisor has exited (any outcome) and every alert is
  posted, once it has posted a "gone" alert, or when a newer launch replaced its launch;
* when the next `launch` starts (earlier timers are stopped and recorded);
* on `runner.py stop`: at once if no supervisor is running; if one is, the timer keeps
  watching until the supervisor exits between issues and then stops itself.

Stopped timers are recorded under `timers` in `watchdog.json`. Manual fallback, for example
after a reboot or with the foreground backend (pass the Linear credential variable by name
if the workspace uses `token_env`):

```bash
systemd-run --user --unit=linear-runner-my-batch-manual-watchdog --on-active=10min \
  --on-unit-active=10min --property=WorkingDirectory=/absolute/path/to/linear-runner \
  /usr/bin/python3 /absolute/path/to/linear-runner/runner.py watchdog --batch my-batch
systemctl --user list-timers 'linear-runner-*'                      # inspect
systemctl --user stop linear-runner-my-batch-manual-watchdog.timer   # remove after the batch
```

## Stop, recovery and continuation

```bash
B=<batch id or file>
python3 runner.py status --batch $B      # phase, active issue/step, blocks, deferred, pending recovery, supervisor.json, STOP, watchdog timer
python3 runner.py stop --batch $B        # stop between issues (durable STOP marker); see "Watchdog" for its timer
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
`--repin-contract`, `--accept-contract-drift` (both read the live issue) and `repin-config`
(Linear name resolution), and is written to
`state.json` and the append-only, hash-chained `<state dir>/recovery-log.jsonl`. None accepts work or resets history, usage, repairs or
escalation. `--then continue` (the default) lets the supervisor continue the batch after
the recovered issue finishes, just as a fresh launch would; `--then stop` stops after it
(writing STOP again, so a later bare continuation needs `--clear-stop`).

| Situation | Command (then `launch --batch $B`) |
| --- | --- |
| Resume the active issue from its saved step (for example a blocked worker) | `recover resume --batch $B --reason R --authorized-by A [--note-file F] [--then stop]` |
| A repair finished `blocked`, or was interrupted, or checks still fail at `validate`, and you fixed the check configuration or environment: re-run the checks on the current source, no model, no repair slot | `recover revalidate --batch $B --reason R --authorized-by A [--then stop]` |
| A repair finished `blocked` and the worker should try again with your note (uses the next repair slot) | `recover resume --batch $B --note-file F --reason R --authorized-by A` |
| Re-run only the independent review of the frozen commit | `recover review --batch $B --reason R --authorized-by A [--redeliver] [--repin-contract] [--note-file F]` |
| Soft-budget checkpoint | `recover budget --batch $B --phase P --input-tokens N --output-tokens N --tool-calls N --reason R --authorized-by A` |
| Publication or its read-back failed after acceptance | `recover publish --batch $B --reason R --authorized-by A` |
| After acceptance (step `publish` or `done`) the contract check stops, but the issue changed only outside the accepted criteria and scope | `recover publish --batch $B --accept-contract-drift --reason R --authorized-by A` |
| Set an issue aside and continue with the others | `recover defer --batch $B --issue ISSUE [--restore-worktree] [--keep-commit] --reason R --authorized-by A` |
| Restore a deferred, parked issue | `recover resume --batch $B --issue ISSUE --reason R --authorized-by A` |
| Withdraw a recovery that was not launched | `recover cancel --batch $B --reason R --authorized-by A` |
| Adopt a changed configuration and/or a newer runner commit (paused or stopped batch; applied at once, then record the recovery the state needs) | `recover repin-config --batch $B --reason R --authorized-by A` |
| A lifecycle post failed after acceptance | `recover resume --batch $B --reason R --authorized-by A` |

Details: `--note-file` text is stored under the issue's `operator-notes/` with its hash
and appended to later model prompts; it does not change acceptance criteria.
`revalidate` needs an active issue stopped at `repair` or `validate` whose checks already
ran (a repair that finished `blocked` or was interrupted, or checks that still failed). The
launch that carries it out sets the step back to `validate` and runs the checks on the
current worktree source without any model call; the repair count and escalation stay as
they are. Passing checks continue to commit, delivery, review and publication; failing
checks enter the normal repair loop with the repairs that are left (an unchanged failure
still stops at once). Only the repair and review model phases may run after it. At a repair
that finished `blocked`, a plain `resume` is refused with a pointer to `revalidate` or to
`resume --note-file F`, which gives the worker one more repair (the next slot, with the
single escalation) and is refused once every repair is used. An interrupted repair cannot
be resumed; `revalidate` or `defer` are its recoveries.
`--repin-contract` adopts an edited live issue before acceptance only, keeping the
previous intake and issue beside it. After acceptance the one re-pin is `recover publish
--accept-contract-drift`: it reads the live issue and re-pins the contract only if the
acceptance criteria (the checklist items the reviewer accepted) and every scope field
(`description`, `projectId`, `assigneeId`, `projectMilestone`, and the issue IDs of the
`blocks`, `blockedBy` and `duplicateOf` relations) are byte-identical to the accepted snapshot; the
description may differ only by publication's ticks (`[x]`/`[X]`). Otherwise it refuses and
names the changed fields: a changed criterion or scope needs a new independent review
(`recover review --repin-contract`), which runs only at the review step. **Known limitation:**
no recovery moves an accepted issue (step `publish` or `done`) back to the review step, so
after acceptance a changed criterion or scope field can only be restored in Linear, or the
issue deferred. On success it keeps the accepted description pinned, stores the previous snapshot
as `issue-before-<R-id>.json` (and `issue-json-before-<R-id>.json`), and records the old and
new contract hashes and the changed field names in `active.contract_repins`, the recovery
record and the recovery log. Like every `publish` recovery it runs no model. `--redeliver` re-runs delivery for the frozen commit
and keeps the old packet as `delivery-superseded-<id>`. `review` allows only the review
model phase and `publish` allows none. `budget` moves the checkpoint into
`budget_reconciliations` and records the new limits as that phase's allowance for this
issue. `defer --restore-worktree` parks uncommitted work in a Git ref; `--keep-commit`
continues on top of an unaccepted controller commit. A parked issue can be restored only
when HEAD has not moved since it was parked, or when it was parked cleanly at `implement`.

**Re-pinning the configuration.** `recover repin-config` is how a paused or stopped batch
adopts an edited private configuration (for example a check that gains `allow_empty`) and/or
a newer runner commit, which otherwise refuse every resume. It is allowed only while no
supervisor or worker is alive and the batch is paused or has a STOP marker, and only when no
recovery is pending (that recovery was recorded against the old configuration: cancel it,
re-pin, record it again). It reloads every layer, resolves the Linear project and assignee
names live and requires them to resolve to the pinned IDs, then:

* refuses any change to the batch identity, which needs a new batch `id`: the batch id, the
  issue allowlist and its order, the project file, the Linear project, workspace and
  assignee names, the resolved project and assignee IDs, the worktree, the branch and the
  state directory;
* refuses a changed check definition (or check environment) while the active issue is at
  `commit`, `delivery`, `review`, `publish` or `done`, whose evidence was validated by the
  pinned checks (finish or defer it first);
* keeps the previous pin as `resolved-config-before-<R-id>.json`, writes the new
  `resolved-config.json` and the new fingerprint into `state.json`, and removes the reuse
  cache entries of checks whose definition changed (the old cache is kept as
  `check-cache-before-<R-id>.json`); history, usage, repairs, escalation, blocks and stops
  are untouched;
* records the old and new fingerprints, the runner commit and dirty flag old → new and each
  changed key, for example `checks[pytest-extended].allow_empty: absent → true`, in
  `state.config_repins`, `state.recoveries` and the recovery log.

It is applied when recorded and is never pending, so it composes with the one pending
recovery: record `repin-config` first, then the recovery the state needs, then launch. For
a check that should have allowed an empty selection:

```bash
python3 runner.py recover repin-config --batch $B --reason R --authorized-by A
python3 runner.py recover revalidate --batch $B --reason R --authorized-by A
python3 runner.py launch --batch $B
```

A stopped batch with nothing paused needs only `repin-config` and then `launch --clear-stop`.
To undo a re-pin, restore the files and re-pin again; every pin stays recorded.

Verify no previous child is alive before recovering (recoveries and launch refuse a live
recorded PID). Never delete state or reset the worktree to clear a failure. The lock
protects one state directory on one host only; the transient unit survives logout (with
lingering enabled) but not a reboot, and it never restarts itself.

State written by an earlier runner version has a different configuration fingerprint and
is refused; adopt the new runner with `recover repin-config` while the batch is paused or
stopped, or finish it with the runner version that created it. Never edit state.

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
