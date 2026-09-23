# Controller development

This repository owns the reusable Linear-to-Codex issue runner, configuration examples, guidance profiles and maintained usage documentation. Keep project-specific behavior in configuration/guidance rather than the shared engine.

Follow your own configured workflow for tracking changes: record substantive changes in a matching issue with the executing session and validation evidence, and keep related guidance in sync.

Run `python3 -m unittest -v`, plus offline `validate-config` for affected batches (at least `python3 runner.py validate-config --home examples/home --batch examples/home/batches/example.json`), before committing. Live issue dispatch is not a unit test; do not launch unrelated work to verify controller changes. Preserve existing state/logs and unrelated work. Keep run reports, prompts from executed sessions and private artifacts outside this source repository. Record commit/push/merge state and limitations in the issue handoff.

There is one engine (`runner.py`) and one configuration model (README "Configuration layers").
Keep policy in `registry/`, keep private hosts/workspaces/projects/batches out of this repository,
and keep every tracked file free of private identifiers; `test_public_tree.py` enforces this (UUIDs, absolute home/user paths, lab or host names, Linear document URLs, and values from the local private home when one exists). Batches start with
`python3 runner.py launch` (see [prompts/launch-batch.md](prompts/launch-batch.md)), which exits after startup
confirmation; recoveries use the named `recover` commands, never ad hoc scripts or state edits;
workers continue their dispatched issue.
