# Controller development

This repository owns the reusable Linear-to-Codex issue runner, configuration examples, guidance profiles and maintained usage documentation. Keep project-specific behavior in configuration/guidance rather than the shared engine.

Follow your own configured workflow for tracking changes: record substantive changes in a matching issue with the executing session and validation evidence, and keep related guidance in sync.

After changing `templates/` or `linear_runner/linear/messages.py`, run `python3 render_samples.py` and commit `docs/template-samples.md` (a test checks it is current). Linear comments are for people: plain sentences, no JSON or tables.

Run `python3 -m unittest -v` from the checkout root, plus offline `validate-config` for affected batches (at least `python3 runner.py validate-config --home examples/home --batch examples/home/batches/example.json`), before committing. Live issue dispatch is not a unit test; do not launch unrelated work to verify controller changes. Preserve existing state/logs and unrelated work. Keep run reports, prompts from executed sessions and private artifacts outside this source repository. Record commit/push/merge state and limitations in the issue handoff.

There is one engine (`linear_runner/engine/runner.py`, run through the root `runner.py`) and one configuration model (README "Configuration layers").
Keep policy in `registry/`, keep private hosts/workspaces/projects/batches out of this repository,
and keep every tracked file free of private identifiers; `tests/test_public_tree.py` enforces this (UUIDs, absolute home/user paths, lab or host names, Linear document URLs, and values from the local private home when one exists). Batches start with
`python3 runner.py launch --batch <file>` (README "Running a batch"), which exits after startup
confirmation; an issue-level block pauses the batch unless the batch sets `supervision.on_block:
"continue_independent"` or a `defer issue when ...` rule in the issue matches; recoveries use the named
`recover` commands, never ad hoc scripts or state edits;
workers continue their dispatched issue.

## Where things live

- `runner.py` (checkout root): the command entry point; systemd units, the watchdog timer and `attention.command_prefix` run it by path, so keep it at the root and keep it a thin shim over `linear_runner/cli.py`. `render_samples.py` is the same kind of shim.
- `linear_runner/cli.py`: argument parsing and command dispatch.
- `linear_runner/config.py`: layered configuration, schemas, `${runner_root}` (the checkout root, not the package), fingerprint and runner identity.
- `linear_runner/engine/`: the per-issue state machine (`runner.py`), check outcomes and delivery integrity (`delivery.py`), intake packets (`intake.py`).
- `linear_runner/backends/`: the model-backend interface and registry (`__init__.py`) and all Codex-specific code (`codex.py`: argv, JSONL events, evidence, model catalog). The engine reaches a model CLI only through this interface; keep CLI flags and event names out of the engine.
- `linear_runner/linear/`: the Linear client, comment templates and ledger (`updates.py`), comment builders (`messages.py`), stops and notifier (`attention.py`).
- `linear_runner/supervision/`: launch and preflight, supervisor, recoveries, watchdog, decision rules.
- `linear_runner/reporting/`: offline records, trajectory, measurement, terminal report, template samples.
- `registry/`, `schema/`, `templates/`, `prompts/`, `examples/`, `testdata/`: data at the checkout root; code finds them through `linear_runner.config.RUNNER_ROOT`. Editing `registry/` or guidance changes configuration fingerprints.
- `tests/`: mirrors the package; shared fixtures (fake Linear, temporary private home, `CHECKOUT`) in `tests/fixtures.py`.
