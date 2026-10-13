"""``runner.py status --board``: one compact record of a batch for a progress board (W-442).

``status --json`` carries the full text of every saved issue and every recorded event (about
70 KB for one batch of the §2.10 batches); a board that redraws every few seconds uses a few
fields of it. This record holds those fields and what the state alone does not give: a title
for an issue the batch has not claimed, the time of the first launch, and each issue's time
and tokens. It has no issue description and no event list.

Model-free and read-only, as ``wait`` is: it reads the state directory (what ``status`` reads,
the launch records and ``titles.json``) and the saved records of the issues' run directories,
writes nothing, takes no lock and makes no Linear call.

Time and tokens are the trajectory report's own, from ``trajectory.from_roots`` on the run
directories the state names: ``report --runs`` on the same directories gives the same figures
with the same bounds, and an invocation without a recorded finish is left out of both.

Per issue: ``stage`` is ``done``, ``deferred``, ``queued`` or the step of the active issue;
``summary`` is the accepted result's summary for a done issue (``null`` when the state was
written before the history kept it) and the latest result's for an active or set-aside one;
``repairs`` is the repair count of the state for an active or set-aside issue and the report's
count of finished repair attempts for a done one.
"""
from __future__ import annotations

from pathlib import Path

from linear_runner.engine.runner import TITLES_NAME
from linear_runner.reporting import records, trajectory
from linear_runner.reporting.records import USAGE_KEYS

SCHEMA = "linear-runner.board/1"


def run_directories(state):
    """``{issue: run directory}`` of every issue the batch has claimed: done, set aside
    (parked) or active; the directories the terminal trajectory report reads."""
    runs = {entry["issue_id"]: entry.get("run_dir") for entry in state.get("history", [])}
    for issue, parked in (state.get("parked") or {}).items():
        if isinstance(parked, dict) and (parked.get("active") or {}).get("run_dir"):
            runs[issue] = parked["active"]["run_dir"]
    if state.get("active"):
        runs[state["active"]["issue_id"]] = state["active"]["run_dir"]
    return runs


def launch_times(root):
    """``(first, last)`` launch time of the batch from its launch records, or ``(None, None)``.
    The state names the latest launch only, so a batch launched again would change its place."""
    times = [(records.read_json(path) or {}).get("at") for path in sorted((Path(root) / "launches").glob("*.json"))]
    times = sorted(time for time in times if isinstance(time, str))
    return (times[0], times[-1]) if times else (None, None)


def record(config, report):
    """The board record of ``config``'s batch; ``report`` is ``cli.status_report(config)``."""
    root = Path(config["state_dir"])
    history = {entry["issue_id"]: entry for entry in report.get("history", [])}
    active = report.get("active") or {}
    parked = report.get("parked") or {}
    deferred = report.get("deferred") or {}
    supervisor = report.get("supervisor") or {}
    pending = report.get("pending_recovery") or {}
    titles = (records.read_json(root / TITLES_NAME) or {}).get("issues") or {}
    first, last = launch_times(root)
    runs = [run for run in dict.fromkeys(run_directories(report).values()) if run and Path(run).is_dir()]
    usage = {s["issue"]: s for s in trajectory.from_roots(runs, issues=config["issues"])["summaries"]}
    issues = []
    for identifier in config["issues"]:
        done = history.get(identifier)
        is_active = active.get("issue_id") == identifier
        # The issue as the state holds it while it is not done: active, or set aside with its work.
        held = active if is_active else (parked.get(identifier) or {}).get("active") or {}
        saved = held.get("issue") or (report.get("issue_cache") or {}).get(identifier) or {}
        known = titles.get(identifier) or {}
        blocks = [block.get("event") for block in (report.get("blocks") or {}).get(identifier, [])]
        spent = usage[identifier]
        files = (done.get("deliverables") or [] if done else
                 [*(held.get("deliverables") or []), *(held.get("delivery_deliverables") or [])])
        issues.append({
            "id": identifier,
            "title": known.get("title") or saved.get("title"),
            "url": known.get("url") or saved.get("url")
                   or f"https://linear.app/{config['linear_workspace']}/issue/{identifier}",
            "stage": ("done" if done else active.get("step") if is_active
                      else "deferred" if identifier in deferred else "queued"),
            "repairs": spent["repairs"] if done else held.get("repairs", 0),
            "blocked": {"worker": blocks.count("worker_blocked"), "review": blocks.count("review_blocked")},
            "summary": done.get("summary") if done else (held.get("last_result") or {}).get("summary"),
            "accepted_at": done.get("completed_at") if done else None,
            "commit": done.get("commit") if done else None,
            "files": [Path(item["path"]).name for item in files],
            "elapsed_seconds": spent["issue_elapsed_seconds"],
            "tokens": {key: spent["totals"][key] for key in USAGE_KEYS},
            "pending_invocations": spent["pending"]})
    return {"schema": SCHEMA,
            "batch": {"id": config["batch_id"], "phase": report.get("phase"),
                      "supervisor": {"status": supervisor.get("status"), "outcome": supervisor.get("outcome")},
                      "first_launch_at": first, "last_launch_at": last,
                      "stop_marker": report.get("stop_marker"),
                      "pending_recovery": {"id": pending.get("id"), "kind": pending.get("kind")} if pending else None,
                      "error": report.get("error"),
                      "lineage": config["lineage"]},
            "issues": issues}
