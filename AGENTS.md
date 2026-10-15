# Controller development

This repository owns the reusable Linear-to-model issue runner, configuration examples, guidance profiles and maintained usage documentation. Keep project-specific behavior in configuration/guidance rather than the shared engine.

Follow your own configured workflow for tracking changes: record substantive changes in a matching issue with the executing session and validation evidence, and keep related guidance in sync.

After changing `templates/` or `linear_runner/linear/messages.py`, run `python3 render_samples.py` and commit `docs/template-samples.md` (a test checks it is current). Linear comments are for people: plain sentences, no JSON or tables, except the usage tables the runner itself writes into the run-summary and batch-finished comments (`templates/README.md`); worker and reviewer drafts follow the plain rule.

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

README "Layout" maps every file and module. The rules that come with the layout:

- `runner.py` (checkout root): the command entry point; systemd units, the watchdog timer and `attention.command_prefix` run it by path, so keep it at the root and keep it a thin shim over `linear_runner/cli.py`. `render_samples.py` is the same kind of shim.
- `${runner_root}` is the checkout root, not the package. Data stays there (`registry/`, `schema/`, `templates/`, `prompts/`, `examples/`, `testdata/`), and code finds it through `linear_runner.config.RUNNER_ROOT`. Editing `registry/` or guidance changes configuration fingerprints.
- `linear_runner/backends/`: all Codex-specific code (`codex.py`) and all Claude Code-specific code (`claude.py`) behind one model-backend interface (`__init__.py`). The engine reaches a model CLI only through this interface; keep CLI flags and event names out of the engine.
- `mods/runner-board/`: the progress board, a Claude Code mod (README "Progress board"); the root `.claude-plugin/marketplace.json` lists it. It reads the runner only through `status --board`, and no worker session loads it. Its tests are not part of the unit suite: run `claude plugin validate mods/runner-board` and `claude plugin test mods/runner-board` after changing it or the board record. `hooks/status-board.fixture.ts` is written by `tests/reporting/test_board.py`, never by hand.
- `tests/`: mirrors the package; shared fixtures (fake Linear, temporary private home, Git repository, `CHECKOUT`) in `tests/fixtures.py`.
