"""Named, recorded recovery commands.

Each command runs under the project lock, checks the saved state exactly, records who
authorized it and why (``--reason``, ``--authorized-by``) in ``state.json`` and in the
append-only, hash-chained ``recovery-log.jsonl``, and leaves a *pending recovery* that the
next ``launch`` consumes after checking that the state is still exactly as recorded. If a
STOP marker existed when the recovery was recorded, its hash is recorded too, and that
launch clears exactly that marker without ``--clear-stop``. ``--then continue`` (default)
continues the batch after the recovered issue; ``--then stop`` stops after it.
No command accepts work, deletes history or resets usage, repair or escalation counters.

* ``resume``  - continue the saved active issue from its saved step (or restore a
  deferred, parked issue with ``--issue``). Optional ``--note-file`` (owner text given to
  later model phases) and ``--repin-contract`` (adopt an edited live issue before acceptance).
* ``review``  - re-run only the independent review of the frozen commit, optionally after
  re-running delivery (``--redeliver``, the previous packet is kept).
* ``budget``  - reconcile a soft-budget checkpoint with an explicitly recorded new allowance.
* ``publish`` - reconcile publication of an already accepted review; no model may run.
* ``defer``   - set an issue aside and let the queue continue with independent issues.
* ``cancel``  - withdraw a pending recovery that has not been launched (recorded too).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from config import write_json
import intake as intake_module
from runner import (IssueBlocked, git, fingerprint, issue_contract, now, run_id, review_criteria,
                    validate_review_result)

LOG_NAME = "recovery-log.jsonl"
KINDS = ("resume", "review", "budget", "publish", "defer", "cancel")
# Steps before independent acceptance, where an edited issue contract may be re-pinned.
PRE_ACCEPTANCE = ("implement", "validate", "commit", "delivery", "review")
PARK_REF = "refs/linear-runner/parked"


class RecoveryError(RuntimeError):
    pass


def _sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def append_log(root, entry):
    """Append one JSON line; each line carries the SHA-256 of the previous line."""
    path = Path(root) / LOG_NAME
    lines = path.read_text().splitlines() if path.exists() else []
    entry = dict(entry, seq=len(lines) + 1, previous_sha256=_sha(lines[-1]) if lines else None)
    with path.open("a") as log:
        log.write(json.dumps(entry, sort_keys=True) + "\n")
        log.flush()
        os.fsync(log.fileno())
    return entry


def verify_log(root):
    """Return the entries after checking the hash chain; raise if a line was edited or removed."""
    path = Path(root) / LOG_NAME
    lines = path.read_text().splitlines() if path.exists() else []
    entries = []
    for index, line in enumerate(lines):
        entry = json.loads(line)
        expected = _sha(lines[index - 1]) if index else None
        if entry.get("seq") != index + 1 or entry.get("previous_sha256") != expected:
            raise RecoveryError(f"{LOG_NAME} line {index + 1} does not continue the hash chain")
        entries.append(entry)
    return entries


def expected_state(state):
    """The exact state shape a recovery was recorded against (checked again at launch)."""
    active = state.get("active")
    return {"phase": state.get("phase"),
            "active": {"issue_id": active["issue_id"], "step": active["step"]} if active else None,
            "history": [h["issue_id"] for h in state.get("history", [])],
            "deferred": sorted(state.get("deferred", {}))}


def _record(runner, kind, *, reason, authorized_by, then, details, pending=True):
    state = runner.state
    identifier = details.pop("id")
    marker = runner.root / "STOP"
    record = {"id": identifier, "kind": kind, "at": now(), "reason": reason.strip(),
              "authorized_by": authorized_by.strip(), "then": then, "details": details,
              "expected": expected_state(state), "host": os.uname().nodename,
              "stop_marker_sha256": _sha(marker.read_bytes()) if marker.exists() else None}
    append_log(runner.root, dict(record, event="recorded"))
    state.setdefault("recoveries", []).append(record)
    if pending:
        state["pending_recovery"] = {k: record[k] for k in ("id", "kind", "then", "expected", "stop_marker_sha256")}
        state["pending_recovery"]["redeliver"] = bool(details.get("redeliver"))
    runner.save()
    return record


def _preflight(runner, reason, authorized_by):
    """Checks that must pass before a recovery changes anything."""
    for name, value in (("--reason", reason), ("--authorized-by", authorized_by)):
        if not isinstance(value, str) or not value.strip():
            raise RecoveryError(f"{name} is required and must not be blank")
    if runner.state.get("pending_recovery"):
        raise RecoveryError(f"Recovery {runner.state['pending_recovery']['id']} is already pending; launch it first")
    runner.verify_config()
    pid = runner.state.get("child_pid")
    if pid and Path(f"/proc/{pid}").exists():
        raise RecoveryError(f"Previous worker PID {pid} may still be alive; inspect before recovery")
    if not runner.state_path.exists():
        raise RecoveryError("This batch has no saved state to recover")


def _active(runner, steps=None):
    active = runner.state.get("active")
    if not active:
        raise RecoveryError("No active issue is saved")
    if steps is not None and active["step"] not in steps:
        raise RecoveryError(f"Active {active['issue_id']} is at step {active['step']!r}; this recovery needs {list(steps)}")
    return active


def _note(runner, active, identifier, note_file, reason, authorized_by):
    text = Path(note_file).expanduser().read_text().strip()
    if not text:
        raise RecoveryError("--note-file is empty")
    directory = Path(active["run_dir"]) / "operator-notes"; directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{identifier}.md"
    path.write_text(text + "\n")
    note = {"id": identifier, "authorized_by": authorized_by.strip(), "reason": reason.strip(), "text": text,
            "path": str(path), "sha256": _sha(path.read_bytes())}
    active.setdefault("operator_notes", []).append(note)
    return {k: note[k] for k in ("path", "sha256")}


def _repin(runner, active, identifier):
    """Adopt the edited live issue before acceptance; the previous intake is preserved."""
    if active["step"] not in PRE_ACCEPTANCE:
        raise RecoveryError("The contract can be re-pinned only before independent acceptance")
    live = runner.linear.issue(active["issue_id"])
    runner.verify_issue(live)
    runner.verify_live_state(live, active["step"])
    from rules import RuleError, parse_rules
    try:
        parse_rules(live.get("description", ""), review_criteria(live))
    except RuleError as error:
        raise RecoveryError(f"Edited issue has invalid decision rules: {error}") from None
    run = Path(active["run_dir"])
    write_json(run / f"issue-before-{identifier}.json", active["issue"])
    intake = run / "intake.json"
    if intake.exists():
        (run / f"intake-before-{identifier}.json").write_bytes(intake.read_bytes())
        packet = json.loads(intake.read_text())
        if packet.get("schema") == intake_module.SCHEMA:
            if (run / "issue.json").exists():
                (run / f"issue-json-before-{identifier}.json").write_bytes((run / "issue.json").read_bytes())
            write_json(run / "issue.json", live)
            packet.update(issue=intake_module.issue_view(live),
                          acceptance_criteria=intake_module.unchecked_criteria(live.get("description")),
                          issue_snapshot=intake_module.file_reference(run / "issue.json"))
        else:
            packet["issue"] = live
        write_json(intake, packet)
    old, old_criteria = active["contract"], review_criteria(active["issue"])
    active["issue"], active["contract"] = live, issue_contract(live)
    runner.state.setdefault("issue_cache", {})[active["issue_id"]] = live
    return {"old_contract": old, "new_contract": active["contract"],
            "old_criteria": old_criteria, "new_criteria": review_criteria(live)}


def recover_resume(runner, *, reason, authorized_by, then="continue", note_file=None, repin=False, issue=None):
    _preflight(runner, reason, authorized_by)
    identifier = "R-" + run_id()
    details = {"id": identifier}
    if issue:
        details.update(unpark(runner, issue, identifier))
    elif runner.state.get("active"):
        active = _active(runner)
        if active.get("budget_exceeded"):
            raise RecoveryError("A soft-budget checkpoint needs `recover budget` with an explicit allowance")
        if active["step"] == "repair":
            raise RecoveryError("An interrupted repair consumed its slot; inspect it, this phase has no generic recovery")
    elif runner.state.get("phase") != "paused":
        raise RecoveryError("Nothing to resume: no active issue and the controller is not paused")
    active = runner.state.get("active")
    if active:
        details.update(issue=active["issue_id"], step=active["step"], session_id=active.get("session_id"))
        if repin:
            details["contract"] = _repin(runner, active, identifier)
        if note_file:
            details["note"] = _note(runner, active, identifier, note_file, reason, authorized_by)
        runner.save(active=active)
    elif repin or note_file:
        raise RecoveryError("--repin-contract and --note-file need an active issue")
    return _record(runner, "resume", reason=reason, authorized_by=authorized_by, then=then, details=details)


def recover_review(runner, *, reason, authorized_by, then="continue", note_file=None, repin=False, redeliver=False):
    _preflight(runner, reason, authorized_by)
    active = _active(runner, ("review",))
    if active.get("budget_exceeded"):
        raise RecoveryError("A soft-budget checkpoint needs `recover budget` with an explicit allowance")
    runner.verify_frozen(active)
    identifier = "R-" + run_id()
    details = {"id": identifier, "issue": active["issue_id"], "commit": active["commit"], "redeliver": redeliver,
               "prior_reviews": sorted(p.parent.name for p in Path(active["run_dir"]).glob("review-*/session.json"))}
    if repin:
        details["contract"] = _repin(runner, active, identifier)
    if note_file:
        details["note"] = _note(runner, active, identifier, note_file, reason, authorized_by)
    runner.save(active=active)
    return _record(runner, "review", reason=reason, authorized_by=authorized_by, then=then, details=details)


def recover_budget(runner, *, reason, authorized_by, phase, limits, then="continue", note_file=None):
    _preflight(runner, reason, authorized_by)
    active = _active(runner)
    exceeded = active.get("budget_exceeded")
    if not exceeded:
        raise RecoveryError(f"Active {active['issue_id']} has no soft-budget checkpoint")
    if exceeded["phase"] != phase:
        raise RecoveryError(f"The checkpoint is for phase {exceeded['phase']!r}, not {phase!r}")
    registry = runner.policy["phases"]["phases"][phase]["budget"]
    if set(limits) != set(registry) or any(not isinstance(v, int) or v < 1 for v in limits.values()):
        raise RecoveryError(f"Give every budget limit explicitly as a positive integer: {sorted(registry)}")
    identifier = "R-" + run_id()
    previous = active.get("budget_allowances", {}).get(phase)
    reconciliation = {"id": identifier, "phase": phase, "checkpoint": exceeded,
                      "previous_limits": previous["limits"] if previous else registry, "new_limits": limits,
                      "reason": reason, "authorized_by": authorized_by, "at": now()}
    # The checkpoint moves to an append-only list; observed usage stays in session records.
    active.setdefault("budget_reconciliations", []).append(reconciliation)
    active.setdefault("budget_allowances", {})[phase] = {"limits": limits, "recovery": identifier}
    del active["budget_exceeded"]
    details = {"id": identifier, "issue": active["issue_id"], "step": active["step"], "phase": phase,
               "observed": exceeded["observed"], "previous_limits": reconciliation["previous_limits"],
               "new_limits": limits}
    if note_file:
        details["note"] = _note(runner, active, identifier, note_file, reason, authorized_by)
    runner.save(active=active)
    return _record(runner, "budget", reason=reason, authorized_by=authorized_by, then=then, details=details)


def recover_publish(runner, *, reason, authorized_by, then="continue"):
    _preflight(runner, reason, authorized_by)
    active = _active(runner, ("publish", "done"))
    try:
        validate_review_result(active.get("accepted_result"), active["issue"], active["commit"])
    except RuntimeError as error:
        raise RecoveryError(f"Saved acceptance does not validate; publication cannot be reconciled: {error}") from None
    runner.verify_frozen(active)
    identifier = "R-" + run_id()
    details = {"id": identifier, "issue": active["issue_id"], "step": active["step"], "commit": active["commit"],
               "accepted_result_sha256": _sha(json.dumps(active["accepted_result"], sort_keys=True))}
    return _record(runner, "publish", reason=reason, authorized_by=authorized_by, then=then, details=details)


def recover_defer(runner, *, reason, authorized_by, issue, restore_worktree=False, keep_commit=False):
    _preflight(runner, reason, authorized_by)
    if issue not in runner.config["issues"]:
        raise RecoveryError(f"{issue} is not in the batch allowlist")
    if issue in [h["issue_id"] for h in runner.state["history"]]:
        raise RecoveryError(f"{issue} is already completed")
    if issue in runner.state.get("deferred", {}):
        raise RecoveryError(f"{issue} is already deferred")
    identifier = "R-" + run_id()
    details = {"id": identifier, "issue": issue}
    active = runner.state.get("active")
    if active and active["issue_id"] != issue:
        # Otherwise the next launch would resume the other issue without its own recovery.
        raise RecoveryError(f"{active['issue_id']} is active; resume or defer it before deferring {issue}")
    if active:
        details["park"] = park(runner, active, identifier, allow_restore=restore_worktree, keep_commit=keep_commit)
    defer(runner, issue, cause={"recovery": identifier, "reason": reason, "authorized_by": authorized_by})
    return _record(runner, "defer", reason=reason, authorized_by=authorized_by, then="continue", details=details)


def recover_cancel(runner, *, reason, authorized_by):
    """Withdraw an unconsumed pending recovery. Its record, and any state change it already
    made (a note, a re-pinned contract, an allowance, a deferral), stay recorded."""
    pending = runner.state.get("pending_recovery")
    if not pending:
        raise RecoveryError("No pending recovery to cancel")
    runner.state["pending_recovery"] = None
    try:
        _preflight(runner, reason, authorized_by)
    finally:
        runner.state["pending_recovery"] = pending
    cancellation = {"at": now(), "reason": reason.strip(), "authorized_by": authorized_by.strip()}
    append_log(runner.root, dict(cancellation, event="cancelled", id=pending["id"]))
    for record in runner.state.get("recoveries", []):
        if record["id"] == pending["id"]:
            record["cancelled"] = cancellation
    runner.save(pending_recovery=None)
    return dict(cancellation, id=pending["id"], kind="cancel")


# --- Deferral and parking --------------------------------------------------------

def defer(runner, issue, *, cause):
    runner.state.setdefault("deferred", {})[issue] = dict(cause, at=now())
    runner.save()


def _snapshot_commit(repo):
    """Commit every non-ignored file (tracked and untracked) without touching the index."""
    with tempfile.TemporaryDirectory() as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))
        run = lambda *args: subprocess.run(["git", "-C", str(repo), *args], env=env, check=True,
                                           capture_output=True, text=True).stdout.strip()
        run("read-tree", "HEAD")
        run("add", "--all")
        tree = run("write-tree")
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=linear-runner", "-c",
                               "user.email=linear-runner@localhost", "commit-tree", tree, "-p", "HEAD", "-m",
                               "linear-runner parked uncommitted work"],
                              check=True, capture_output=True, text=True).stdout.strip()


def park(runner, active, identifier, *, allow_restore, keep_commit):
    """Move the active issue aside so independent issues can run on a clean worktree.

    Uncommitted work is preserved in a Git ref (and the manifest's patch/tar snapshot)
    before the worktree is restored; a controller commit is kept only on explicit request.
    """
    repo = runner.repo
    head = git(repo, "rev-parse", "HEAD")
    dirty = bool(git(repo, "status", "--porcelain"))
    record = {"issue": active["issue_id"], "step": active["step"], "head": head,
              "starting_commit": active["starting_commit"], "dirty": dirty, "at": now()}
    if head != active["starting_commit"]:
        if dirty:
            raise RecoveryError("The worktree has a controller commit and uncommitted changes; reconcile manually")
        if not keep_commit:
            raise RecoveryError(f"{active['issue_id']} has an unaccepted controller commit {head[:12]}; "
                                "continuing on top of it needs --keep-commit")
        record["kept_commit"] = head
    elif dirty:
        if not allow_restore:
            raise RecoveryError(f"{active['issue_id']} left uncommitted changes; --restore-worktree parks them "
                                "in a Git ref and restores a clean worktree")
        runner.manifest(active)
        record["fingerprint"] = fingerprint(repo)
        commit = _snapshot_commit(repo)
        ref = f"{PARK_REF}/{runner.config['batch_id']}/{active['issue_id']}/{identifier}"
        git(repo, "update-ref", ref, commit)
        git(repo, "reset", "-q", "--hard", "HEAD")
        git(repo, "clean", "-q", "-fd")
        if git(repo, "status", "--porcelain") or git(repo, "rev-parse", "HEAD") != head:
            raise RecoveryError("Worktree restore after parking did not produce a clean baseline")
        record.update(parked_ref=ref, parked_commit=commit)
    else:
        runner.manifest(active)
    runner.state.setdefault("parked", {})[active["issue_id"]] = {"active": active, "park": record}
    runner.save(active=None, last_commit=head, child_pid=None)
    return record


def unpark(runner, issue, identifier):
    """Restore a parked issue as the active issue (exact preconditions only)."""
    parked = runner.state.get("parked", {}).get(issue)
    if not parked:
        raise RecoveryError(f"{issue} is not parked")
    if runner.state.get("active"):
        raise RecoveryError("Another issue is active; finish or defer it first")
    repo = runner.repo
    if git(repo, "status", "--porcelain"):
        raise RecoveryError("Restoring a parked issue needs a clean worktree")
    active, record = parked["active"], parked["park"]
    head = git(repo, "rev-parse", "HEAD")
    details = {"issue": issue, "park": record}
    if record.get("parked_commit") or record.get("kept_commit"):
        if head != record["head"]:
            raise RecoveryError(f"HEAD moved since {issue} was parked ({record['head'][:12]} -> {head[:12]}); "
                                "restore it manually")
        if record.get("parked_commit"):
            git(repo, "read-tree", "-u", "--reset", record["parked_commit"])
            git(repo, "reset", "-q")
            if fingerprint(repo) != record["fingerprint"]:
                raise RecoveryError("Restored worktree differs from the parked snapshot")
    elif head != active["starting_commit"]:
        if active["step"] != "implement":
            raise RecoveryError(f"{issue} was parked at step {active['step']!r}; it can restart only at implement")
        details["starting_commit"] = {"old": active["starting_commit"], "new": head}
        active["starting_commit"] = head
    del runner.state["parked"][issue]
    runner.state.get("deferred", {}).pop(issue, None)
    runner.save(active=active)
    return details


def block_record(runner, active, error, launch_id):
    """Record an issue-level block with the criteria the model reported unsatisfied."""
    result = active.get("last_result") or {}
    unsatisfied = [e.get("criterion") for e in result.get("acceptance", []) if isinstance(e, dict)
                   and e.get("satisfied") is not True and isinstance(e.get("criterion"), str)]
    blocks = runner.state.setdefault("blocks", {}).setdefault(active["issue_id"], [])
    entry = {"id": f"{active['issue_id']}#{len(blocks) + 1}", "event": getattr(error, "event", "blocked"),
             "step": active["step"], "error": str(error), "unsatisfied": unsatisfied, "at": now(),
             "launch_id": launch_id}
    blocks.append(entry)
    runner.save()
    return entry


__all__ = ["IssueBlocked", "KINDS", "RecoveryError", "append_log", "verify_log", "expected_state", "park", "unpark",
           "defer", "block_record", "recover_resume", "recover_review", "recover_budget", "recover_publish",
           "recover_defer", "recover_cancel"]
