Real Claude Code 2.1.281 `claude -p --output-format stream-json --verbose` output recorded by the
W-190 probes (tiny prompts, `claude-opus-5-5` medium and an unknown model ID), scrubbed: UUIDs
replaced by `id-N`, host paths by `/tmp/...`, thinking signatures by `SIGNATURE`, and the
slash-command/skill listings removed. Nothing else was changed.

* `reviewer-denied-write.jsonl`: reviewer flags; one Bash `touch` denied by `dontAsk`, then a
  schema-bound result (`structured_output`).
* `worker-resume.jsonl`: worker flags; `--resume` of an earlier session (same session ID).
* `error-unknown-model.jsonl`: an API 404 error result (`is_error: true`, exit code 1).

`review-summary.md` is the `summary` of one real review (W-195) by `claude-sonnet-5` at medium
effort on Claude Code 2.1.281, run through `Runner.model_phase` (the reviewer flags and the
shared prompt, including `updates.draft_rules`) on a public toy repository; it is the draft the
runner saved as `outbox/001-review.md` and held for posting. Only the host path prefix of the
`Evidence:` line was replaced by `/tmp/toy-review/`. It must keep passing the draft lint.
