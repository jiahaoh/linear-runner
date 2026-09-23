# linear-runner

A sequential controller for explicitly authorized Linear issue batches. Deterministic
Python owns scheduling, gates, Linear synchronization, checks, the Git commit and
publication. Codex CLI is invoked only for three bounded phases: implementation,
repair and an independent read-only review. No extra Python packages are required.

The engine has no project-specific prompts, owner, workspace or toolchain. Policy lives
in the public `registry/`; everything private (hosts, workspaces, projects, batches)
lives in a private configuration home outside this repository. Your own workflow and
authorization rules belong in project or batch guidance files. Outer launchers follow
[prompts/launch-batch.md](prompts/launch-batch.md).

Requires Python 3.10+, Git, a POSIX host (tested on Linux), an authenticated Codex CLI and
a Linear credential reachable through an environment variable or the Codex credential
cache. CLI event parsing was established on Codex CLI 0.154.0; probe changed versions
before unattended use.

## Layout

| Path | Contents |
| --- | --- |
| `runner.py` | The engine and CLI (`validate-config`, `dry-run`, `run`, `status`, `stop`, `clear-stop`) |
| `config.py` | Layered loading, schema validation, `${variable}` substitution, name resolution pinning |
| `linear_client.py` | Direct HTTPS JSON-RPC client for the official Linear MCP endpoint |
| `report.py` | Standalone terminal HTML/JSON report |
| `registry/` | Public policy defaults; each file's `notes` explain its values |
| `schema/` | JSON schemas for every registry file and configuration layer |
| `examples/home/` | Placeholder private home: site, workspace, project and batch files |
| `prompts/` | Generic worker guidance and the outer launch procedure |
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
| Site | `executables` (must include `codex`), `variables`, `state_root`, `artifact_root`, `model_catalog` |
| Workspace | `slug` (matches the file name), `auth` (exactly one of `token_env` or `credentials_file`, optional `timeout_seconds`), `assignee` (`"me"` or an exact name/email; default `"me"`), optional `states` renames |
| Project | `workspace`, `linear_project` (exact Linear project name), `repo`, `artifact_owner`, `retention`, optional `backup_status`, `guidance_files`, optional `context_files`, `identity_files`, `check_environment`, `checks`, optional `delivery_checks` |
| Batch | `id`, `project`, `issues` (ordered allowlist), `terminal_issue`, `branch`, optional `worktree` (defaults to the project `repo`), `guidance_files` (appended after the project's), `required_done`, `human_gates` |

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
Codex, so the Linear project and assignee remain unresolved names in its report. `dry-run`
and `run` resolve the exact project name and the assignee to IDs (no match or more than one
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
checkout are not distinguished; run batches from a clean commit.

## Running a batch

1. Read the live issues, dependencies, workflow and repository instructions. Record scope,
   acceptance, data/environment/resource limits and permitted operations in Linear.
2. Prepare a clean dedicated worktree and `codex/` branch from the chosen baseline. Use one
   active controller per project.
3. Copy `examples/home/` to your private home, replace every placeholder and use real
   validation commands. Add project or batch guidance files as needed.
4. Validate offline, dry-run against Linear, then inspect one completed issue before
   continuing with a newly introduced profile or host.

```bash
python3 runner.py validate-config --batch ~/.config/linear-runner/batches/my-batch.json
python3 -m unittest -v
python3 runner.py dry-run --batch ~/.config/linear-runner/batches/my-batch.json
python3 runner.py run --batch ~/.config/linear-runner/batches/my-batch.json --max-issues 1
python3 runner.py status --batch ~/.config/linear-runner/batches/my-batch.json
```

`--max-issues` limits implementation dispatch; the terminal report runs after the last
issue in that limit without an extra slot. If more work remains, the runner checkpoints
and exits.

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
7. **Delivery.** Optional `delivery_checks` receive `RUNNER_DELIVERY_CONTEXT` with the
   revision and successful checks. A failure stops before review.
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
checkpoints the issue for explicit reconciliation; resume does not reset it. Usage is the
per-session maximum of cumulative counters summed over sessions; it is not billed cost.

One stable execution-summary comment per issue and one terminal-report comment are
upserted by marker. Each write first saves a local pending record; a failed write keeps
the batch from advancing, and resume reconciles the marker comment or an already
completed publish without rerunning models or rewriting the description.

## Stop, supervision and recovery

```bash
python3 runner.py stop --batch <batch file>
python3 runner.py status --batch <batch file>
# Inspect saved work, child processes, Git, issue ownership and validation logs.
python3 runner.py clear-stop --batch <batch file>
python3 runner.py run --batch <batch file> --resume --max-issues 1
```

`stop` writes a durable marker; the active issue reaches a checkpoint and no next issue
starts. For immediate interruption, stop the supervising service: the controller
terminates its child process group, preserves state and records a blocked terminal report.
Verify no previous child is alive before resuming. Never delete state or reset the
worktree to clear a failure. Run unattended under a host supervisor such as
`systemd-run --user` with automatic restart disabled. The lock protects one state
directory on one host only.

State written by an earlier runner version has a different configuration fingerprint and
is refused; finish or reconcile it with the runner version that created it rather than
editing state.

Parallel workers, distributed leasing, automatic restart and scientific acceptance are
not implemented. Tests use temporary Git repositories with fake Codex and Linear
boundaries; they make no network calls.

## License

MIT License; see [LICENSE](LICENSE).
