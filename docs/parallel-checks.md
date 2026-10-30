# Parallel checks: design note (W-617)

Status: proposed, for the owner's review. Nothing here is implemented. The implementation is
release 3.4.0.

## The problem

The controller runs a validation's checks one after another (`run_checks`), each pinned to one
CPU. A host with more cores sits mostly idle during validation.

Measured on the largest project so far, on a host with eight physical cores and 124 GB:

| Replay of 101 recorded validations | In sequence | 2 cores | 3 cores | 4 cores | 6 cores |
| --- | --- | --- | --- | --- | --- |
| Check time, all validations | 40.2 h | 26.7 h | 23.2 h | 21.9 h | 21.4 h |
| The longest validation (17 checks) | 120.7 min | 63.8 min | 45.0 min | 35.9 min | |

The replay keeps the default tier first and places the extended checks longest first. It assumes
each check takes as long as it did alone. Most of the gain is at a batch's end: the 76 validations
under 30 minutes hold 10.1 h, and four cores bring them only to 9.7 h, because they mostly run one
default check.

Four checks were then run at the same time by hand, in one worktree, each on its own physical core
with its own temporary directory, while another batch was running on the host:

| Check | Alone | Four at once | Wall | CPU time | Peak RSS |
| --- | --- | --- | --- | --- | --- |
| Fast tier | 23:37 | 24:16 | +2.8% | +3.1% | 2.57 GiB |
| Slow, synthetic | 18:02 | 18:28 | +2.3% | +2.5% | 2.76 GiB |
| Slow, other | 8:36 | 9:12 | +7.1% | +7.1% | 1.09 GiB |
| Documentation build | 2:13 | 2:36 | +17% | +1.5% | 0.42 GiB |

Together they took 24.3 minutes against 52.5 in sequence, with no failure. The documentation
build's wall figure reflects writing to a network mount and sharing its core with a repeated test;
later builds on local disk took under two minutes. The hand run did not use this engine, so it does
not cover stopping and recovery, and it did not run the learned checks or go through `uv run`.

Three things follow from these numbers:

* Memory does not limit this: four such checks sum to under 12 GiB on a 124 GB host.
* Cores do: two hyper-threads of one core are not two cores, and their numbering need not be
  adjacent. Read `/sys/devices/system/cpu/cpu*/topology/thread_siblings_list`.
* Beyond four slots there is little to gain, because the longest chain is a default check followed
  by the longest extended check.

## The decision

Each batch has a fixed list of check CPUs, one per physical core. The default tier runs as it does
today. The extended checks a validation selects then run at the same time, one per CPU, longest
first. With no list configured, nothing changes.

## Configuration

**Site.** One reserved site variable, which a batch overrides in its own `variables` as it does
`cpu_list` today:

```json
{"variables": {"check_cpus": "2,4,5,6"}}
```

It is a variable rather than a new site section because the site schema refuses unknown keys and
batches re-read their files on every command, so a new section would break an older runner's `status`,
`stop` and `recover`. Like every variable it is in the configuration fingerprint: changing it for a
paused batch takes `recover repin-config`, which is the recorded way to change a batch's resources.
`launcher.cpu_list` keeps pinning the supervisor; it is not the check list.

**Project.** Two built-in variables replace `${cpu_list}` in check commands:

```json
{"command": ["${taskset}", "-c", "${check_cpu}", "${time}", "-v", "..."]}
```

* `${check_cpu}` is the CPU given to this run of the check.
* `${all_check_cpus}` is the whole list, for a default check that uses every core itself, such as
  `pytest -n 4`. Default checks run before the extended ones start, so the list is free. An extended
  check that names it is refused.

Both resolve to the literals `{check_cpu}` and `{all_check_cpus}`, which `validate` replaces when it
starts the process, as it does for `{run_dir}`. They are accepted only in the `command` of `checks` and
`delivery_checks`, and not as its first element. Anywhere else (`check_environment`, a check's
`environment` or `cwd`, `launcher.environment`) they stay undefined variables, because only `command` is
replaced at run time and model sessions inherit `check_environment`. Three things follow:

* The CPU is not in the check's definition, so the evidence key does not depend on which CPU ran it.
* A runner older than this release refuses a project file that uses them, with "undefined variable".
  No `interface_version` bump is needed: existing project files stay valid.
* The issue packet carries `config["checks"]`, so a worker would see the literals. The packet replaces
  both with the batch's `cpu_list`, so that a command a worker copies stays on the worker's own CPU.

**Who runs where:**

| Check | Runs | `{check_cpu}` | `{all_check_cpus}` |
| --- | --- | --- | --- |
| Selected extended, not base parity | at the same time | a free CPU of the list | refused |
| Default | in sequence, where selected | the list's first CPU | the whole list |
| Base parity | in sequence, where selected | the first CPU | refused, being extended |
| Launch baseline (default checks only) | in sequence | the first CPU | the whole list |
| Delivery | in sequence, after the validation | the first CPU | the whole list |

Without `check_cpus`, both mean `launcher.cpu_list`, and every check runs in sequence, so a project can
switch its files before any site sets a list.

**Per-check directories.** A new optional project key names the variables that are per check:

```json
{"check_directories": ["TMPDIR", "MPLCONFIGDIR", "NUMBA_CACHE_DIR"]}
```

For a check that `run_checks` runs, other than base parity, the engine sets each one to
`<value>/<validation directory name>/<check's configuration index>` and creates it. The name is unique
per validation, so two batches with one base never meet and a retry never reuses a failed run's files,
and an index cannot hold `/` or `..` as a check's name can. It removes the directory after a passed or
empty check and keeps it after a failed one; the number of repairs bounds what stays. The launch baseline,
delivery checks and model sessions get the value unchanged. Unset, this key stays out of the fingerprint.

**Refusals.** Configuration loading refuses, for every command: a list that does not parse; a CPU also in
`launcher.cpu_list`; the uses of the two variables named above; and `${check_cpu}` with neither
`check_cpus` nor `launcher.cpu_list` set. `validate-config` and the launch preflight also read the host and
refuse a CPU that is not online and two CPUs of one physical core, where the sysfs file can be read.

## The engine

**Selection does not change.** `run_checks` still walks the checks in W-552's order. Default checks
run where they are selected, and a failure among them still sends the extended checks to the next
validation. Base-parity checks also still run where they are selected, on the first CPU. The one
change: an extended check that would run is appended to a pending list instead of starting. Keys are
computed during selection, when nothing is running, and recomputed for `ran` at the end, as today.

**Then the pending checks run**, in a new `run_pending`, longest first by `expected_seconds`, with
checks that have no record first. W-552's shortest-first order stays for the default tier, where the
first failure decides. An extended failure does not stop the others today and does not here, so the
order only affects the total. In the replay, shortest first costs 25.4 h on three cores and longest
first 23.2 h.

**One thread and a poll loop, no thread pool.** The main thread starts a check whenever a CPU is free,
and polls every running child about once a second. It writes the records, the cache and the state
itself, so nothing needs a lock. A SIGTERM handler still runs where it does today, and the code can be
tested with fake commands. `validate`'s body splits into starting a process and finishing a record,
and both paths use them.

**Files and records.** Each check keeps its directory, `check-0.log` and `checks.json`, with
`/usr/bin/time -v` inside its command. `checks.json` for the validation stays in configuration order.
A record gains `cpu` when `check_cpus` is set. A check's timeout runs from its own start, so
waiting for a CPU does not count against it.

**Stop and recovery.** Check children live in `self.check_children`; `self.child` remains the model
session's. A timeout stops only its own process group: today's `validate` calls `stop_child`, which would
end the whole pool. `stop_child` sends SIGTERM to every process group, waits on one shared deadline rather
than ten seconds each, then sends SIGKILL. The state gains `child_pids` beside `child_pid`, and the five
readers use one helper that accepts both, so a state written by an older release still works:
`recovery.py` (two), `launcher.py`, `supervisor.py`, `runner.py`. The write at `recovery.py`'s reconcile
clears both.

**Reports.** `check_seconds` and `validation_seconds` remain sums, which no longer match elapsed time.
A new `check_wall_seconds` is the union of the checks' intervals, and `totals()["seconds"]` and the
run summary's "Checks" row use it. The audit rows `totals` reads then need `finished_at`.

## W-617's questions

| Question | Answer |
| --- | --- |
| Budget | A static list per batch (in the fingerprint), not a host budget with admission. Memory does not constrain this, one process per core cannot oversubscribe, and overlapping lists between two batches only slow them down. The operator keeps lists apart; the engine does not check other batches in this release. |
| Declared needs per check | None. Every check takes one CPU. A check that uses more takes the default tier and `${all_check_cpus}`. |
| Peak RSS in the cache | Not needed for a static list. Logs already hold it. |
| Order | Shortest first in the default tier, longest first among the extended checks. |
| Fail fast | Unchanged. A default failure holds the extended checks back; an extended failure stops nothing. |
| Isolation | `check_directories`. The uv cache stays shared, since uv documents concurrent use as safe; the hand run called the environment's Python directly, so the canary is the first test of this. Removal after a pass matters: per-check temporary directories hold up to 3.4 GiB. |
| Evidence | Byte-based keys unchanged, records in configuration order. A test compares one slot with four. |
| Shared resources | Logs are separate files on the mount. Checks never overlap a batch's own worker or reviewer. |
| Project limits | A project that states "one CPU" per validation changes its own guidance. |

## Later users

`run_pending` takes jobs (command, directory, environment, CPU, timeout, log) and knows nothing of
check names or evidence. A benchmark runner could use it once such experiments are defined. It would
need a GPU resource, a memory budget for runs beyond the checks' sizes, per-job manifests and resume
after a stop. None of this is built now, and experiment results must not share evidence records with
checks.

## Not in this release

* A host-level scheduler, admission by memory, or a check of other batches' lists.
* `-n` within a check. A project can do that itself once `${all_check_cpus}` exists.
* Stopping running checks when one fails.
* Parallel launch baselines or parallel base-parity checks.

## Rollout

1. Implement in release 3.4.0. Canary tier: canary batch, because the engine changes.
2. Run the canary batch, with every check named so that all of them run in the pool, and one stop and
   recovery during the pool. The owner inspects it.
3. Switch the project files and the site only when no batch on an older release is running or paused.
   Batches re-read their files on every command, so an older runner would refuse a switched project
   file, and a new site variable would trip a paused batch's fingerprint.
