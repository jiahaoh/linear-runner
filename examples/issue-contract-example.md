## Purpose

Add a `--since` filter to the example tool's `history` command so people can list recent
changes without paging through everything. Tracking issue: TEAM-100.

Shared contract: `shared-contract.md` as pinned by the batch configuration. This issue
follows it without exceptions.

## Deliverables

* `history --since <ISO date>` in `tool/cli.py`, with help text.
* Tests in `tests/test_history.py`.
* One paragraph in `docs/usage.md`.

## Acceptance criteria

- [ ] `tool history --since 2026-01-01` lists only entries on or after that date (shown by a test).
- [ ] An invalid date exits with status 2 and a one-line message naming the expected format.
- [ ] Without `--since`, the output is byte-identical to the previous release (golden-file test).
- [ ] `python3 -m unittest -v` passes, including the new tests.
- [ ] `docs/usage.md` documents the option with one example command.
- [ ] No other command's behavior or output changes (diff limited to the files above).

## Decision rules (optional)

```linear-runner-rules
defer issue when worker blocked 2 times on the same criterion
```

## Exclusions

* No time-zone conversion; dates are compared as UTC.
