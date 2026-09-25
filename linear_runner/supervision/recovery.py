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
  At a repair that finished ``blocked`` it needs ``--note-file``: the worker then gets the
  next repair slot with the note. An interrupted repair cannot be resumed.
* ``revalidate`` - at a ``repair`` or ``validate`` stop after checks ran (a repair that
  finished blocked or was interrupted, or checks still failing): re-run the checks on the
  current worktree source with no model call and without using a repair slot. Passing
  checks continue to commit, delivery, review and publication; failing checks enter the
  normal repair loop with the remaining repair budget.
* ``review``  - re-run only the independent review of the frozen commit, optionally after
  re-running delivery (``--redeliver``, the previous packet is kept).
* ``repair``  - at a ``review`` stop after a blocked review: send the reviewer's findings (each
  unmet criterion with its evidence, the summary and limitations, plus an optional
  ``--note-file``) back to the worker as a repair in its issue session. It uses the next slot
  of the shared repair budget. After it the checks run in full, the repair is committed as a
  new controller commit on top of the earlier one (never amended), delivery runs again and a
  fresh independent review assesses the whole issue diff from the starting commit.
* ``budget``  - reconcile a soft-budget checkpoint with an explicitly recorded new allowance.
* ``publish`` - reconcile publication of an already accepted review; no model may run.
  ``--accept-contract-drift`` (at ``publish`` or ``done``) also re-pins the issue contract
  when the live issue changed only outside what the reviewer accepted: the acceptance
  criteria and every scope field (description, project, assignee, milestone, and the issue
  IDs of ``blocks``, ``blockedBy``, ``duplicateOf``) must be byte-identical to the accepted snapshot, otherwise
  it refuses and names the changed fields. It records the old and new contract hashes and
  the changed field names; it never runs a model.
* ``defer``   - set an issue aside and let the queue continue with independent issues.
* ``cancel``  - withdraw a pending recovery that has not been launched (recorded too).
* ``repin-config`` - adopt a changed configuration and/or a newer runner commit for a paused
  or stopped batch. Unlike the others it is applied when recorded and is never pending: it
  re-pins ``resolved-config.json`` (the previous file is kept), records the old and new
  fingerprints, the runner commit and the changed keys, and invalidates reused check
  evidence whose definition changed. Record it first, then the recovery the paused state
  needs (for example ``revalidate``), then launch. It refuses while a recovery is pending
  and never changes the batch identity (issue allowlist and order, project, workspace,
  assignee, worktree, branch, state directory, resolved Linear IDs).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from linear_runner.config import write_json
from linear_runner.engine import intake as intake_module
from linear_runner.engine.runner import (CONTRACT_RELATIONS, IssueBlocked, contract_fields, git, fingerprint,
                                         issue_contract, now, published_issue, review_criteria,
                                         review_repair_waiting, run_id, validate_review_result)

LOG_NAME = "recovery-log.jsonl"
KINDS = ("resume", "revalidate", "review", "repair", "budget", "publish", "defer", "cancel", "repin-config")
# What repin-config may never change: these define the batch; changing them needs a new batch.
REPIN_FIXED = (("batch_id", "the batch id"), ("issues", "the issue allowlist and its order"),
               ("project_name", "the Linear project"), ("linear_workspace", "the Linear workspace"),
               ("assignee", "the assignee"), ("worktree", "the worktree"), ("branch", "the branch"),
               ("state_dir", "the state directory"), ("project_id", "the resolved Linear project ID"),
               ("assignee_id", "the resolved Linear assignee ID"))
# Steps whose validated evidence would no longer match a changed check definition.
VALIDATED_STEPS = ("commit", "delivery", "review", "publish", "done")
# Steps before independent acceptance, where an edited issue contract may be re-pinned.
PRE_ACCEPTANCE = ("implement", "validate", "commit", "delivery", "review")
PARK_REF = "refs/linear-runner/parked"
# Scope fields the independent review accepted (besides the criteria and the issue id);
# --accept-contract-drift requires each to be byte-identical to the accepted snapshot.
SCOPE_FIELDS = ("description", "projectId", "assigneeId", "projectMilestone")


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
              "config_sha256": state.get("config_sha256"),
              "stop_marker_sha256": _sha(marker.read_bytes()) if marker.exists() else None}
    if not pending:
        record["applied_at"] = record["at"]  # applied when recorded (repin-config); never consumed by a launch
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
        raise RecoveryError("The contract can be re-pinned only before independent acceptance; after it, "
                            "`recover publish --accept-contract-drift` adopts changes outside the accepted criteria "
                            "and scope")
    live = runner.linear.issue(active["issue_id"])
    runner.verify_issue(live)
    runner.verify_live_state(live, active["step"])
    from linear_runner.supervision.rules import RuleError, parse_rules
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


def repair_outcome(state):
    """How the active issue's last dispatched repair ended: ``blocked`` (it finished and
    reported blocked) or ``interrupted`` (no result: killed, timed out, Codex failed).

    New state records ``active.repair_blocked`` (the repair number that ended blocked); state
    written before that field falls back to the issue's latest stop.
    """
    active = state["active"]
    if active.get("repair_blocked") is not None:
        return "blocked" if active["repair_blocked"] == active.get("repairs") else "interrupted"
    stops = [s for s in state.get("stops", []) if s.get("issue") == active["issue_id"]]
    if stops and stops[-1].get("step") == "repair" and stops[-1].get("event") == "worker_blocked":
        return "blocked"
    return "interrupted"


def _repairs_left(runner, active):
    return runner.policy["phases"]["max_repairs"] - active.get("repairs", 0)


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
        if active["step"] == "repair" and review_repair_waiting(active):
            # A `recover repair` stopped before its repair was dispatched (no slot used).
            details["repair_retry"] = {"repairs_used": active["repairs"],
                                       "max_repairs": runner.policy["phases"]["max_repairs"], "review_repair": True}
        elif active["step"] == "repair":
            name, left = active["issue_id"], _repairs_left(runner, active)
            limit = runner.policy["phases"]["max_repairs"]
            if repair_outcome(runner.state) == "interrupted":
                raise RecoveryError(f"The repair of {name} was interrupted and consumed its slot, so it cannot be "
                                    "resumed. `recover revalidate` re-runs the checks on the current source without "
                                    "a model; `recover defer` sets the issue aside")
            if not note_file:
                raise RecoveryError(
                    f"The repair of {name} finished with status blocked, so a plain resume would only repeat it. "
                    "Use `recover revalidate` to re-run the checks without a model after fixing their "
                    "configuration or environment (no repair slot is used), or `recover resume --note-file F` "
                    f"to give the worker a note for one more repair ({left} of {limit} repairs left)")
            if left <= 0:
                raise RecoveryError(f"The repair of {name} finished with status blocked and all {limit} repairs are "
                                    "used; `recover revalidate` re-runs the checks without a model, or `recover "
                                    "defer` sets the issue aside")
            details["repair_retry"] = {"repairs_used": active["repairs"], "max_repairs": limit}
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


def recover_revalidate(runner, *, reason, authorized_by, then="continue"):
    """Re-run validation on the current source: no model call, no repair slot used."""
    _preflight(runner, reason, authorized_by)
    active = _active(runner, ("repair", "validate"))
    if active.get("budget_exceeded"):
        raise RecoveryError("A soft-budget checkpoint needs `recover budget` with an explicit allowance")
    previous = Path(active.get("validation_dir") or "") / "checks.json"
    if not active.get("validation_dir") or not previous.is_file():
        raise RecoveryError(f"{active['issue_id']} has not run its checks yet; `recover resume` runs them")
    from linear_runner.engine.delivery import check_passed
    records = json.loads(previous.read_text())
    identifier = "R-" + run_id()
    details = {"id": identifier, "issue": active["issue_id"], "step": active["step"],
               "repair": repair_outcome(runner.state) if active["step"] == "repair" else None,
               "previous_validation": active["validation_dir"],
               "previously_failing": [r.get("name") for r in records if not check_passed(r)],
               "repairs_used": active.get("repairs", 0), "max_repairs": runner.policy["phases"]["max_repairs"]}
    return _record(runner, "revalidate", reason=reason, authorized_by=authorized_by, then=then, details=details)


def recover_review(runner, *, reason, authorized_by, then="continue", note_file=None, repin=False, redeliver=False):
    _preflight(runner, reason, authorized_by)
    if (runner.state.get("active") or {}).get("step") in ("publish", "done"):
        raise RecoveryError(f"Active {runner.state['active']['issue_id']} is past independent acceptance (step "
                            f"{runner.state['active']['step']!r}); use `recover publish`, with --accept-contract-drift "
                            "when the issue changed only outside the accepted criteria and scope")
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


def latest_review_attempt(active):
    """The attempt directory of ``active``'s latest review (its recorded stage, else the newest
    ``review-*`` directory of the run), or None."""
    stage = next((s for s in reversed(active.get("stages") or []) if s.get("phase") == "review"), None)
    if stage and stage.get("attempt"):
        return Path(stage["attempt"])
    attempts = sorted(Path(active["run_dir"]).glob("review-*"))
    return attempts[-1] if attempts else None


def blocked_review(runner, active):
    """The saved result of ``active``'s latest review if it blocked the issue (status blocked or
    an unmet criterion) for the frozen commit; RecoveryError otherwise."""
    name = active["issue_id"]
    attempt = latest_review_attempt(active)
    if attempt is None:
        raise RecoveryError(f"No independent review of {name} is recorded, so there are no findings to send back; "
                            "`recover review` runs the review")
    path = attempt / "phase-result.json"
    if not path.is_file():
        raise RecoveryError(f"The latest review of {name} ({attempt.name}) saved no result (it was interrupted or "
                            "failed before returning one), so there are no findings to send back; `recover review` "
                            "re-runs it")
    data = path.read_bytes()
    try:
        result = json.loads(data)
    except ValueError:
        result = None
    if not isinstance(result, dict):
        raise RecoveryError(f"The saved review result {path} is not a JSON object; `recover review` re-runs the review")
    if result.get("issue_id") != name or result.get("commit") != active["commit"]:
        raise RecoveryError(f"The saved review result {path} is for {result.get('issue_id')!r} at commit "
                            f"{result.get('commit')!r}, not {name} at the frozen commit {active['commit']}; "
                            "`recover review` reviews the frozen commit")
    entries = result.get("acceptance") if isinstance(result.get("acceptance"), list) else []
    unmet = [e for e in entries if isinstance(e, dict) and e.get("satisfied") is not True]
    if result.get("status") != "blocked" and not unmet:
        raise RecoveryError(f"The latest review of {name} ({attempt.name}) reported no unmet criterion and was not "
                            "blocked, so there are no findings to send back; `recover review` re-runs the review")
    stops = [s for s in runner.state.get("stops", []) if s.get("issue") == name]
    return {"attempt": str(attempt), "result": str(path), "sha256": _sha(data), "status": result.get("status"),
            "unsatisfied": [e.get("criterion") for e in unmet],
            "stop": {k: stops[-1].get(k) for k in ("id", "event", "step")} if stops else None}


def recover_repair(runner, *, reason, authorized_by, then="continue", note_file=None):
    """Send a blocked review's findings back to the worker as a repair (see the module notes).

    Recorded like every recovery; the launch that carries it out moves the active issue from
    ``review`` to ``repair`` (``Supervisor.consume``) and the runner repairs, validates, commits
    on top, re-delivers and reviews afresh."""
    _preflight(runner, reason, authorized_by)
    active = _active(runner)
    name, step = active["issue_id"], active["step"]
    if step != "review":
        hint = (" It is past independent acceptance; use `recover publish`." if step in ("publish", "done") else
                " `recover resume` or `recover revalidate` continue a repair or validation stop.")
        raise RecoveryError(f"`recover repair` sends a blocked independent review's findings back to the worker, so it "
                            f"needs the review step after a blocked review; {name} is at step {step!r}.{hint}")
    if active.get("budget_exceeded"):
        raise RecoveryError("A soft-budget checkpoint needs `recover budget` with an explicit allowance")
    left, limit = _repairs_left(runner, active), runner.policy["phases"]["max_repairs"]
    if left <= 0:
        raise RecoveryError(f"All {limit} repairs of {name} are used, so the review's findings cannot go back to the "
                            "worker; `recover review` re-runs only the review, or `recover defer` sets the issue aside")
    runner.verify_frozen(active)
    review = blocked_review(runner, active)
    identifier = "R-" + run_id()
    details = {"id": identifier, "issue": name, "step": step, "commit": active["commit"],
               "starting_commit": active["starting_commit"], "session_id": active.get("session_id"),
               "review": review, "repairs_used": active.get("repairs", 0), "max_repairs": limit}
    if note_file:
        details["note"] = _note(runner, active, identifier, note_file, reason, authorized_by)
    runner.save(active=active)
    return _record(runner, "repair", reason=reason, authorized_by=authorized_by, then=then, details=details)


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


def _canonical(value):
    return json.dumps(value, sort_keys=True)


def _checklist(description):
    """Every checklist item's text in order, whatever its mark ([ ], [x] or [X])."""
    return re.findall(r"^\s*[-*] \[[ xX]\] (.+)$", description or "", re.M)


def accepted_scope_changes(accepted, live):
    """Names of what the reviewer accepted that differ in ``live``: ``acceptance criteria``,
    the SCOPE_FIELDS and ``relations.<name>`` for CONTRACT_RELATIONS. The description may
    differ from the accepted one only by publication's ticks (``[x]``/``[X]``)."""
    changed = []
    if live.get("id") != accepted.get("id"):
        changed.append("id")
    description = live.get("description") or ""
    if _checklist(description) != _checklist(accepted.get("description")):
        changed.append("acceptance criteria")
    ticked = re.sub(r"^(\s*[-*] )\[X\]", r"\1[x]", description, flags=re.M)
    if description != (accepted.get("description") or "") and ticked != published_issue(accepted)["description"]:
        changed.append("description")
    changed += [f for f in SCOPE_FIELDS[1:] if _canonical(live.get(f)) != _canonical(accepted.get(f))]
    old, new = contract_fields(accepted)["relations"], contract_fields(live)["relations"]
    changed += [f"relations.{k}" for k in CONTRACT_RELATIONS if new[k] != old[k]]  # by issue ID, as in the contract
    return changed


def changed_fields(before, after):
    """Top-level issue fields (and ``relations.<name>``) whose values differ."""
    names = []
    for key in sorted(set(before) | set(after)):
        if key == "relations":
            old, new = before.get(key) or {}, after.get(key) or {}
            names += [f"relations.{k}" for k in sorted(set(old) | set(new)) if _canonical(old.get(k)) != _canonical(new.get(k))]
        elif _canonical(before.get(key)) != _canonical(after.get(key)):
            names.append(key)
    return names


def _accept_drift(runner, active, identifier, reason, authorized_by):
    """Re-pin the contract after acceptance when only fields outside it changed (see module notes)."""
    live = runner.linear.issue(active["issue_id"])
    accepted = active["issue"]
    scope = accepted_scope_changes(accepted, live)
    if scope:
        raise RecoveryError(
            f"{active['issue_id']}: what the independent review accepted changed ({', '.join(scope)}); "
            "--accept-contract-drift re-pins only changes outside the accepted criteria and scope. A changed "
            "criterion or scope field needs a new review against the edited issue (`recover review "
            "--repin-contract`), which runs only at the review step, and no recovery moves an accepted issue back "
            "to review: restore the accepted text or fields in Linear, or set the issue aside (`recover defer`)")
    runner.verify_issue(live)
    runner.verify_live_state(live, active["step"])
    # The accepted description stays pinned (the live one equals it or its ticked form), so
    # publication writes and validates exactly what the reviewer accepted.
    pinned = dict(live, description=accepted.get("description"))
    run = Path(active["run_dir"])
    before = run / f"issue-before-{identifier}.json"
    write_json(before, accepted)
    snapshot = run / "issue.json"
    if snapshot.exists():
        (run / f"issue-json-before-{identifier}.json").write_bytes(snapshot.read_bytes())
        write_json(snapshot, pinned)
    record = {"id": identifier, "at": now(), "step": active["step"], "old_contract": active["contract"],
              "new_contract": issue_contract(pinned), "changed_fields": changed_fields(accepted, pinned),
              "previous_issue": str(before), "reason": reason.strip(), "authorized_by": authorized_by.strip()}
    active.setdefault("contract_repins", []).append(record)
    active["issue"], active["contract"] = pinned, record["new_contract"]
    runner.state.setdefault("issue_cache", {})[active["issue_id"]] = live
    return {k: record[k] for k in ("old_contract", "new_contract", "changed_fields", "previous_issue")}


def recover_publish(runner, *, reason, authorized_by, then="continue", accept_drift=False):
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
    if accept_drift:
        details["accepted_contract_drift"] = _accept_drift(runner, active, identifier, reason, authorized_by)
        runner.save(active=active)
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


# --- Re-pinning the configuration ---------------------------------------------------

def _changed_checks(old, new):
    """Names of checks whose definition (or shared check environment) changed or vanished."""
    before = {c["name"]: c for c in old.get("checks", [])}
    after = {c["name"]: c for c in new.get("checks", [])}
    if old.get("check_environment") != new.get("check_environment"):
        return sorted(before)
    return sorted(name for name, spec in before.items() if after.get(name) != spec)


def recover_repin_config(config, linear, *, reason, authorized_by):
    """Adopt the current configuration (and runner commit) for a paused or stopped batch.

    ``config`` is freshly loaded (``load_config``). Applied at once; see the module notes.
    """
    from linear_runner.config import (RESOLVED_NAME, _with_ids, config_changes, config_fingerprint, read_json,
                                      resolution_names, write_resolved)
    from linear_runner.engine.runner import Runner
    for name, value in (("--reason", reason), ("--authorized-by", authorized_by)):
        if not isinstance(value, str) or not value.strip():
            raise RecoveryError(f"{name} is required and must not be blank")
    root = Path(config["state_dir"])
    pinned_path, state_path = root / RESOLVED_NAME, root / "state.json"
    if not pinned_path.exists() or not state_path.exists():
        raise RecoveryError("This batch has no pinned state to re-pin")
    pinned, state = read_json(pinned_path), read_json(state_path)
    if state.get("config_sha256") != pinned.get("config_sha256"):
        raise RecoveryError(f"state.json and {RESOLVED_NAME} name different configurations; reconcile them first")
    if state.get("pending_recovery"):
        raise RecoveryError(f"Recovery {state['pending_recovery']['id']} is pending and was recorded against the "
                            "pinned configuration; cancel it (recover cancel), re-pin, then record it again")
    status_path = root / "supervisor.json"
    status = read_json(status_path) if status_path.exists() else {}
    if status.get("status") == "running" and status.get("pid") and Path(f"/proc/{status['pid']}").exists():
        raise RecoveryError(f"Supervisor {status.get('launch_id')} (PID {status['pid']}) is still running; "
                            "the configuration can be re-pinned only while the batch is paused or stopped")
    pid = state.get("child_pid")
    if pid and Path(f"/proc/{pid}").exists():
        raise RecoveryError(f"Previous worker PID {pid} may still be alive; inspect before recovery")
    if state.get("phase") != "paused" and not (root / "STOP").exists():
        raise RecoveryError(f"The batch is {state.get('phase')!r} without a STOP marker; the configuration can be "
                            "re-pinned only while the batch is paused or stopped (runner.py stop)")
    old = pinned["config"]
    ids = pinned["resolution"]["ids"]
    new = _with_ids(config, ids, f"re-pinned {RESOLVED_NAME}")
    fixed = [label for key, label in REPIN_FIXED if old.get(key) != new.get(key)]
    projects = lambda layers: sorted(k for k in layers if k.startswith("project "))
    if projects(pinned.get("layers", {})) != projects(config["_layers"]):
        fixed.append("the project configuration file")
    if pinned["resolution"]["names"] != resolution_names(config) and not fixed:
        fixed.append("the Linear names")
    if fixed:
        raise RecoveryError("repin-config cannot change " + ", ".join(fixed) + "; those need a new batch id")
    # Linear name resolution, as at launch: the names must still resolve to the pinned IDs.
    live = {"project_id": linear.resolve_project(config["project_name"]),
            "assignee_id": linear.resolve_user(config["assignee"])}
    if live != ids:
        raise RecoveryError(f"Linear now resolves the project/assignee names to {live}, not the pinned {ids}; "
                            "that needs a new batch id")
    old_sha, new_sha = pinned["config_sha256"], config_fingerprint(new)
    if old_sha == new_sha:
        raise RecoveryError("The configuration and runner are unchanged since they were pinned; nothing to re-pin")
    changes = config_changes(old, new)
    invalidated = _changed_checks(old, new)
    active = state.get("active")
    if invalidated and active and active["step"] in VALIDATED_STEPS:
        raise RecoveryError(f"{active['issue_id']} is at step {active['step']!r} with evidence validated by the "
                            f"pinned checks, and the definition of {invalidated} changed; finish or defer it first")
    runner = Runner(new, linear)  # checks the batch identity against state.json
    identifier = "R-" + run_id()
    archived = root / f"resolved-config-before-{identifier}.json"
    archived.write_bytes(pinned_path.read_bytes())
    cache_path = root / "check-cache.json"
    dropped = []
    if cache_path.exists() and invalidated:
        cache = read_json(cache_path)
        dropped = sorted(name for name in cache if name in invalidated)
        if dropped:
            (root / f"check-cache-before-{identifier}.json").write_bytes(cache_path.read_bytes())
            write_json(cache_path, {k: v for k, v in cache.items() if k not in dropped})
    write_resolved(new)
    details = {"id": identifier, "old_config_sha256": old_sha, "new_config_sha256": new_sha,
               "runner": {"old": old.get("runner"), "new": new.get("runner")}, "changes": changes,
               "changed_checks": invalidated, "invalidated_check_evidence": dropped,
               "previous_resolved_config": str(archived),
               "active": {"issue": active["issue_id"], "step": active["step"]} if active else None}
    runner.state["config_sha256"] = new_sha
    runner.state.setdefault("config_repins", []).append(
        {"id": identifier, "at": now(), "authorized_by": authorized_by.strip(), "reason": reason.strip(),
         "old_config_sha256": old_sha, "new_config_sha256": new_sha, "runner": details["runner"], "changes": changes,
         "invalidated_check_evidence": dropped, "previous_resolved_config": str(archived)})
    record = _record(runner, "repin-config", reason=reason, authorized_by=authorized_by, then=None, details=details,
                     pending=False)
    return record


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
           "defer", "block_record", "repair_outcome", "recover_resume", "recover_revalidate", "recover_review", "recover_repair", "recover_budget",
           "recover_publish", "blocked_review",
           "recover_defer", "recover_cancel", "recover_repin_config"]
