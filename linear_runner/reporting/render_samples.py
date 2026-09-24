"""Render one sample Linear comment per human-review template into docs/template-samples.md.

The samples use fictional fixture data (issues TEAM-10 to TEAM-15, placeholder paths) and
the same builders the runner uses, so each sample is exactly the body Linear would get.

    python3 render_samples.py            # rewrite docs/template-samples.md
    python3 render_samples.py --check    # exit 1 if the file is out of date
"""
from __future__ import annotations

import argparse
import sys

from linear_runner.config import RUNNER_ROOT
from linear_runner.linear import messages
from linear_runner.linear import updates

ROOT = RUNNER_ROOT
OUTPUT = ROOT / "docs" / "template-samples.md"
RUN = "/absolute/path/to/runs/TEAM-12/20260923T101500Z-1a2b3c4d"
STATE = "/absolute/path/to/controller/demo-batch"
CTX = {"batch": "demo-batch", "batch_arg": "demo-batch", "home": None,
       "prefix": "python3 /absolute/path/to/linear-runner/runner.py", "mention": "",
       "branch": "codex/demo-batch", "max_repairs": 2}
WORKTREE = "/absolute/path/to/worktrees/demo-batch"
DELIVERABLES = [{"path": f"{WORKTREE}/reports/qc_report.html",
                 "description": "QC report with per-tile spot counts and error bars"},
                {"path": f"{WORKTREE}/docs/calibration.md", "description": "How the calibration table is read"},
                {"path": f"{RUN}/delivery/packet/review.html",
                 "description": "file from the delivery packet"}]
CRITERIA = ["The QC report shows per-tile spot counts with error bars",
            "The pipeline reads the calibration table from the configured data folder",
            "Existing regression tests still pass"]
BLOCKED_RESULT = {
    "issue_id": "TEAM-12", "status": "blocked", "commit": "",
    "summary": ("I could not find the calibration table that the second criterion names. The configured data "
                "folder only has raw tiles, and the older pipeline hard-coded the values instead."),
    "acceptance": [{"criterion": CRITERIA[0], "satisfied": True, "evidence": "qc_report.html renders all tiles"},
                   {"criterion": CRITERIA[1], "satisfied": False,
                    "evidence": "calibration.csv is not in the configured data folder"},
                   {"criterion": CRITERIA[2], "satisfied": True, "evidence": "pytest -q passed (212 tests)"}],
    "limitations": []}
READY_RESULT = dict(BLOCKED_RESULT, status="ready", summary=(
    "Added per-tile spot counts with bootstrap error bars to the QC report and read the calibration table "
    "from the configured data folder."), acceptance=[dict(e, satisfied=True, evidence="checked") for e in
                                                     BLOCKED_RESULT["acceptance"]],
                    limitations=["Error bars use 200 bootstrap samples; more would take several minutes per tile"])
REPAIR_BLOCKED_RESULT = dict(BLOCKED_RESULT, summary=(
    "The only failing check is pytest-extended: it selected no tests because this issue moved those tests into "
    "the default run. Bringing the marker back would undo the issue, so I changed nothing."), acceptance=[
    dict(BLOCKED_RESULT["acceptance"][2], satisfied=False,
         evidence="pytest-extended exited 5 with every test deselected")])
REVIEW_RESULT = dict(READY_RESULT, summary=(
    "The committed work meets all three criteria and the report reads well. The error bars match a manual "
    "bootstrap on two tiles."))

PROGRESS_DRAFT = """The spot-count section of the QC report now renders for all tiles; no action is needed.

**What changed**
I added per-tile spot counts to the QC report and a small helper that computes bootstrap error bars. The report renders for the fixture dataset in about ten seconds.

**Next**
Wire the calibration table into the pipeline and run the focused tests.

**Risks**
The calibration table may not be in the configured data folder; I will check next.

Evidence: /absolute/path/to/runs/TEAM-12/20260923T101500Z-1a2b3c4d/qc_report.html
"""
READY_DRAFT = """TEAM-12 is ready for validation: the QC report has per-tile error bars and the pipeline reads the calibration table; no action is needed.

**What was done**
Per-tile spot counts with bootstrap error bars are in the QC report. The pipeline now reads calibration.csv from the configured data folder instead of hard-coded values.

**How it was checked**
I ran the focused QC tests and the full regression suite once; both passed.

**Limitations**
Error bars use 200 bootstrap samples to keep the report fast.

Evidence: /absolute/path/to/runs/TEAM-12/20260923T101500Z-1a2b3c4d/implement-20260923T101500Z-5e6f7a8b
"""
BLOCKED_DRAFT = """I cannot finish TEAM-12 because the calibration table it names is not in the data folder, and I need the owner to say where it lives.

**What is blocking**
The second criterion asks the pipeline to read the calibration table from the configured data folder. That folder only has raw tiles, and the older pipeline hard-coded the values.

**What was tried**
I searched the data folder and the project notes and found no calibration file.

**What is needed**
Either the path of the calibration table or permission to write one from the hard-coded values.
"""
REVIEW_DRAFT = """I accept TEAM-12: all three criteria are met and nothing needs the owner's attention.

**Assessment**
The QC report shows per-tile spot counts with error bars, and the values match a manual bootstrap on two tiles. The pipeline reads the calibration table from the configured folder, and the regression suite passes on the committed revision.

**Concerns**
The error bars use 200 bootstrap samples; that is fine for QC but not for publication figures.
"""


def samples():
    """(title, template file, author, when posted, issue, kind, body)."""
    attempt = RUN + "/implement-20260923T101500Z-5e6f7a8b"
    def stage(phase, model, effort, backend="codex", profile="Standard", source="pool default"):
        return {"phase": phase, "profile": profile, "model": model, "effort": effort, "backend": backend,
                "model_source": source}
    luna = {p: stage(p, "gpt-6-luna", "max") for p in ("implement", "repair")}
    astra = stage("review", "gpt-6-astra", "medium")
    plan = {"implement": luna["implement"], "repair": luna["repair"], "review": astra}
    label = "issue label model:claude-opus-5-5"
    opus = {p: stage(p, "claude-opus-5-5", "medium", "claude", source=label) for p in ("implement", "repair")}
    claude_plan = {"implement": opus["implement"], "repair": opus["repair"], "review": astra}
    team15 = "/absolute/path/to/runs/TEAM-15/20260924T091500Z-3c4d5e6f"
    records = [{"name": "regression", "exit_code": 1}, {"name": "docs", "exit_code": 0, "reused": True},
               {"name": "pytest-extended", "exit_code": 5, "status": "empty", "allow_empty": True}]
    recovery = {"id": "R-20260923T120000Z-9c8d7e6f", "kind": "resume", "authorized_by": "Owner", "then": "continue",
                "reason": "The calibration table is at data/calibration.csv; the note tells the worker",
                "details": {"issue": "TEAM-12", "step": "implement"}}
    block = {"id": "TEAM-13#2", "event": "worker_blocked", "step": "implement",
             "error": "Worker reported blocked: the viewer needs a browser the host does not have"}
    worker_blocked_13 = dict(BLOCKED_RESULT, issue_id="TEAM-13", summary=(
        "The viewer check needs a headless browser, and none is installed on the host."), acceptance=[
        {"criterion": "The viewer opens the QC report", "satisfied": False, "evidence": "no browser on the host"}])
    status = f"{STATE}/supervisor.json"
    items = [
        ("Claim", "claim.md", "runner", "when work on an issue starts", "TEAM-12", "claim",
         messages.claim(CTX, issue="TEAM-12", plan=plan, check_count=2, criteria_count=3, run_dir=RUN)),
        ("Claim, Claude worker named by a label", "claim.md", "runner",
         "when work starts on an issue labelled model:claude-opus-5-5", "TEAM-15", "claim",
         messages.claim(CTX, issue="TEAM-15", plan=claude_plan, check_count=2, criteria_count=2, run_dir=team15)),
        ("Progress", "draft-progress.md", "worker", "as soon as the runner sees the draft, while the model still runs",
         "TEAM-12", "progress", messages.draft_post(PROGRESS_DRAFT, "worker", "implement", luna["implement"])),
        ("Ready for validation", "draft-ready.md", "worker", "when the session ends with status ready",
         "TEAM-12", "ready", messages.draft_post(READY_DRAFT, "worker", "implement", luna["implement"])),
        ("Ready for validation, runner fallback", "ready.md", "runner",
         "instead of the worker's note when it is missing or fails the lint", "TEAM-12", "ready",
         messages.ready(CTX, issue="TEAM-12", result=READY_RESULT, attempt=attempt,
                        draft_problem="the first paragraph must be exactly one sentence ending with '.', '!' or '?'",
                        deliverables=DELIVERABLES[:2], stage=luna["implement"])),
        ("Validation result", "validation.md", "runner", "after each validation run (passed, or failed with a repair next)",
         "TEAM-12", "validation", messages.validation(CTX, issue="TEAM-12", records=records, passed=False, repair=1,
                                                      directory=RUN + "/validation-20260923T103000Z-0a1b2c3d",
                                                      repair_stage=luna["repair"])),
        ("Review result", "draft-review.md", "reviewer", "when the independent review accepts the work",
         "TEAM-12", "review", messages.draft_post(REVIEW_DRAFT, "reviewer", "review", astra)),
        ("Review result, runner fallback", "review.md", "runner",
         "instead of the reviewer's note when it fails the lint", "TEAM-12", "review",
         messages.review(CTX, issue="TEAM-12", result=REVIEW_RESULT, attempt=RUN + "/review-20260923T110000Z-4d5e6f7a",
                         draft_problem="missing required section(s): Assessment", stage=astra)),
        ("Done, with deliverables to review", "done.md", "runner", "after Done is published and read back",
         "TEAM-12", "done", messages.done(CTX, issue="TEAM-12", commit="4c44464d4ce9a0b1", criteria_count=3, repairs=1,
                                          run_dir=RUN, deliverables=DELIVERABLES,
                                          stages=[luna["implement"], luna["repair"], astra])),
        ("Done, Claude-backed implementation", "done.md", "runner",
         "after Done is published, for an issue a Claude worker implemented", "TEAM-15", "done",
         messages.done(CTX, issue="TEAM-15", commit="7e6d5c4b3a291807", criteria_count=2, repairs=0, run_dir=team15,
                       stages=[opus["implement"], astra])),
        ("Blocked or stopped, with the worker's blocked note", "blocked.md + draft-blocked.md", "runner (quotes the worker)",
         "when the batch pauses; the issue also gets the Needs input label", "TEAM-12", "blocked",
         messages.blocked(CTX, issue="TEAM-12", classification="needs-decision", event="worker_blocked",
                          error="Worker reported blocked", step="implement", phase="implement", result=BLOCKED_RESULT,
                          draft=BLOCKED_DRAFT, evidence_paths=[RUN, attempt], stage=luna["implement"])),
        ("Blocked or stopped, review not accepted", "blocked.md", "runner (quotes the reviewer)",
         "when the batch pauses on a rejected review", "TEAM-12", "blocked",
         messages.blocked(CTX, issue="TEAM-12", classification="needs-decision", event="review_blocked",
                          error="Independent acceptance is incomplete", step="review", phase="review",
                          who="reviewer", result=dict(BLOCKED_RESULT, summary=(
                              "The QC report has no error bars on the per-tile counts, which the first criterion "
                              "requires."), acceptance=[
                              {"criterion": CRITERIA[0], "satisfied": False,
                               "evidence": "qc_report.html shows counts without error bars"},
                              *[dict(e, satisfied=True) for e in BLOCKED_RESULT["acceptance"][1:]]]), evidence_paths=[RUN, RUN + "/review-20260923T110000Z-4d5e6f7a"],
                          stage=astra)),
        ("Blocked or stopped, environment", "blocked.md", "runner", "when a host or service problem pauses the batch",
         "TEAM-12", "blocked",
         messages.blocked(CTX, issue="TEAM-12", classification="environment", error=(
             "Linear OAuth expired; refresh with the owning CLI and resume"), step="validate",
             evidence_paths=[RUN, f"{STATE}/state.json"])),
        ("Blocked or stopped, Claude token rejected", "blocked.md", "runner",
         "when a Claude session fails to authenticate with the configured long-lived token", "TEAM-15", "blocked",
         messages.blocked(CTX, issue="TEAM-15", classification="environment", error=(
             "Claude failed or did not finish a turn: Claude authentication (oauth-token-file) failed: error result "
             "(api_error, API status 401): Invalid bearer token; see " + team15 + "/implement-20260923T120000Z-5e6f7a8b"),
             step="implement", evidence_paths=[team15, f"{STATE}/state.json"])),
        ("Blocked or stopped, a repair finished blocked", "blocked.md", "runner (quotes the worker)",
         "when a repair ends with status blocked instead of ready", "TEAM-12", "blocked",
         messages.blocked(CTX, issue="TEAM-12", classification="needs-decision", event="worker_blocked",
                          error=("Repair did not report ready: the only failing check is pytest-extended, which "
                                 "selected no tests"), step="repair", phase="repair", repairs=1,
                          result=REPAIR_BLOCKED_RESULT, evidence_paths=[RUN, RUN + "/repair-20260923T104000Z-6a7b8c9d"],
                          stage=luna["repair"])),
        ("Issue deferred", "deferred.md", "runner", "when a decision rule or on_block policy sets an issue aside",
         "TEAM-13", "deferred",
         messages.deferred(CTX, issue="TEAM-13", cause={"rule": "rule-0a1b2c3d4e5f",
                                                        "rule_text": "defer issue when worker blocked 2 times on the same criterion"},
                           block=block, result=worker_blocked_13,
                           evidence_paths=["/absolute/path/to/runs/TEAM-13/20260923T130000Z-2b3c4d5e"])),
        ("Recovery recorded", "recovery.md", "runner", "when a launch carries out a recorded recovery", "TEAM-12",
         "recovery", messages.recovery(CTX, record=recovery, step="implement",
                                       note="The calibration table is data/calibration.csv; read it, do not recreate it.",
                                       evidence_paths=[f"{STATE}/recovery-log.jsonl"], stage=luna["implement"])),
        ("Recovery recorded, revalidate", "recovery.md", "runner", "when a launch carries out a revalidate recovery",
         "TEAM-12", "recovery",
         messages.recovery(CTX, record={"id": "R-20260923T121500Z-1d2e3f4a", "kind": "revalidate",
                                        "authorized_by": "Owner", "then": "continue",
                                        "reason": "The pytest-extended check now allows an empty selection",
                                        "details": {"issue": "TEAM-12", "step": "repair"}},
                           step="validate", evidence_paths=[f"{STATE}/recovery-log.jsonl"])),
        ("Recovery recorded, publish after changes outside the accepted scope", "recovery.md", "runner",
         "when a launch carries out `recover publish --accept-contract-drift`", "TEAM-12", "recovery",
         messages.recovery(CTX, record={"id": "R-20260923T123000Z-2e3f4a5b", "kind": "publish",
                                        "authorized_by": "Owner", "then": "continue",
                                        "reason": "A new issue added a related link; the criteria and scope are unchanged",
                                        "details": {"issue": "TEAM-12", "step": "publish",
                                                    "accepted_contract_drift": {"changed_fields": ["relations.relatedTo"]}}},
                           step="publish", evidence_paths=[f"{STATE}/recovery-log.jsonl"])),
        ("Batch finished", "batch-finished.md", "runner", "on the terminal issue and report issues when the batch ends",
         "TEAM-14", "batch-finished",
         messages.batch_finished(CTX, outcome="partial", done=["TEAM-10", "TEAM-11", "TEAM-12"], total=5,
                                 issues=messages.issues_prose(done=["TEAM-10", "TEAM-11", "TEAM-12"],
                                                              deferred=["TEAM-13"], waiting={"TEAM-14": ["TEAM-13"]}),
                                 usage={"sessions": 7, "totals": {"input_tokens": 18_400_000, "cached_input_tokens":
                                                                  15_900_000, "output_tokens": 212_000}},
                                 deferred=["TEAM-13"],
                                 evidence_paths=[f"{STATE}/terminal-report.html", f"{STATE}/terminal-report.json"])),
        ("Batch paused", "batch-paused.md", "runner", "on the terminal issue and report issues when the batch pauses",
         "TEAM-14", "batch-paused",
         messages.batch_paused(CTX, subject="TEAM-12", where="TEAM-12",
                               issues=messages.issues_prose(done=["TEAM-10", "TEAM-11"], paused="TEAM-12",
                                                            pending=["TEAM-13", "TEAM-14"]),
                               evidence_paths=[f"{STATE}/terminal-report.html", f"{STATE}/terminal-report.json"])),
        ("Watchdog alert", "watchdog.md", "runner (model-free watchdog)",
         "when the supervisor vanished without an outcome, or stalled", "TEAM-12", "watchdog",
         messages.watchdog(CTX, condition="gone", subject="TEAM-12", observed=(
             "supervisor.json still says running (launch L-20260923T090000Z-7f8e9d0c, PID 48121 on build-host), but "
             "that process no longer exists and no terminal outcome was written. The last recorded progress was at "
             "2026-09-23 10:42 UTC."), last_update=("From the progress comment: The spot-count section of the QC "
                                                  "report now renders for all tiles; no action is needed."),
             evidence_paths=[status, f"{STATE}/supervisor.log", RUN])),
        ("Done, nothing to review", "done.md", "runner", "after Done is published and read back", "TEAM-11", "done",
         messages.done(CTX, issue="TEAM-11", commit="9a8b7c6d5e4f3a2b", criteria_count=2, repairs=0,
                       run_dir="/absolute/path/to/runs/TEAM-11/20260923T081500Z-0f1e2d3c")),
    ]
    counters = {}
    result = []
    for title, template, author, when, issue, kind, body in items:
        counters[(issue, kind)] = counters.get((issue, kind), 0) + 1
        key = updates.event_key(issue, kind, counters[(issue, kind)])
        result.append((title, template, author, when, updates.with_marker(body, CTX["batch"], key)))
    return result


def render_document():
    lines = ["# Linear comment samples", "",
             "Generated by `python3 render_samples.py` from fictional fixture data; do not edit by hand. Each "
             "sample below is exactly the comment body Linear would receive for that template, including the one "
             "hidden marker line at the end (an HTML comment Linear does not display). Paths, issue IDs and the "
             "owner handle are placeholders; model names are the registry's. Template wording lives in `templates/`.", "",
             "| # | Sample | Template | Author | Posted |", "| --- | --- | --- | --- | --- |"]
    items = samples()
    for index, (title, template, author, when, _) in enumerate(items, start=1):
        lines.append(f"| {index} | {title} | `templates/{template.replace(' + ', '` + `templates/')}` | {author} | {when} |")
    for index, (title, template, author, when, body) in enumerate(items, start=1):
        lines += ["", f"## {index}. {title}", "", f"Template `{template}`, written by the {author}, posted {when}.",
                  "", "---", "", body.rstrip(), "", "---"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the samples file is out of date")
    args = parser.parse_args(argv)
    text = render_document()
    if args.check:
        current = OUTPUT.read_text() if OUTPUT.exists() else ""
        if current != text:
            print(f"{OUTPUT.relative_to(ROOT)} is out of date; run python3 render_samples.py", file=sys.stderr)
            return 1
        return 0
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(text)
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({len(samples())} samples)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
