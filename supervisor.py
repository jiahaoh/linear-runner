"""Generic host supervisor: what a launched unit runs (``runner.py supervise``).

It replaces per-batch supervise scripts. Under the project lock it:

* refuses to start unless the launch preflight for this launch ID passed against the
  current configuration and source, no STOP marker exists, no worker is alive, and a
  paused or unfinished state has a recorded recovery whose expected state still matches;
* completes any missing lifecycle read-back of already accepted issues first;
* finishes the active issue (under the recovery's restrictions), then processes the
  allowlist issue by issue, skipping deferred issues and issues whose Linear
  ``blockedBy`` prerequisites are not Done, and re-checking gates before each issue;
* after every accepted issue re-validates the accepted result, reads Linear Done and the
  checklist back and writes ``lifecycle/<issue>/readback.json`` with hashes (the issue's
  plain-language "done" comment was already posted by the runner);
* posts a NEW "recovery" comment when it carries out a recorded recovery and removes the
  needs-input mark of the stop it recovers;
* stops at planned checkpoints, on STOP, or on a batch-level failure; on an issue-level
  block it applies a matching pre-authorized decision rule or the batch's ``on_block``
  policy (stop, or defer the issue and continue with independent issues);
* writes a terminal report, posts the batch summary as a new comment on the terminal
  issue and ``report_issues``, and, by default, writes a STOP marker when it exits.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import signal

from config import config_fingerprint, read_json, write_json
import messages
from recovery import append_log, block_record, defer, expected_state, park
from rules import RuleError, evaluate, parse_rules
from runner import (IssueBlocked, PHASES, Runner, git, issue_contract, now, project_lock, published_contract_matches,
                    resolve_profile, review_criteria, run_id, validate_review_result)

STATUS_NAME = "supervisor.json"
LIFECYCLE_DIR = "lifecycle"


class SupervisorRefused(RuntimeError):
    """Refused before any dispatch; nothing is written to Linear."""


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pid_alive(pid):
    return bool(pid) and Path(f"/proc/{pid}").exists()


class Supervisor:
    def __init__(self, runner, *, launch_id, stop_after=(), scope="queue"):
        self.r = runner
        self.config = runner.config
        self.root = runner.root
        self.launch_id = launch_id
        supervision = self.config["supervision"]
        self.stop_after = list(dict.fromkeys([*supervision["stop_after"], *stop_after]))
        self.scope = scope
        self.redeliver = False
        self.external = set()

    @property
    def state(self):
        return self.r.state

    def completed(self):
        return [h["issue_id"] for h in self.state["history"]]

    # --- Status and preconditions ---------------------------------------------

    def write_status(self, status, **fields):
        path = self.root / STATUS_NAME
        record = read_json(path) if path.exists() else {}
        if record.get("launch_id") != self.launch_id:
            record = {"launch_id": self.launch_id, "pid": os.getpid(), "host": os.uname().nodename,
                      "started_at": now()}
        record.update(fields, status=status, updated_at=now())
        write_json(path, record)

    def check_preflight(self):
        path = self.root / "preflight" / f"{self.launch_id}.json"
        if not path.is_file():
            raise SupervisorRefused(f"No launch preflight record for {self.launch_id}; start with `runner.py launch`")
        record = read_json(path)
        if not record.get("passed"):
            raise SupervisorRefused("The launch preflight did not pass")
        if record["identities"]["config"]["fingerprint"] != config_fingerprint(self.config):
            raise SupervisorRefused("Configuration changed after the launch preflight")
        if record["identities"]["source"]["head"] != git(self.r.repo, "rev-parse", "HEAD"):
            raise SupervisorRefused("Source revision changed after the launch preflight")

    def admit(self):
        """Exact-state admission; returns the pending recovery consumed by this run, if any."""
        state = self.state
        pending = state.get("pending_recovery")
        if pending:
            if expected_state(state) != pending["expected"]:
                raise SupervisorRefused(f"State changed since recovery {pending['id']} was recorded; record a new recovery")
        elif state.get("phase") == "paused":
            raise SupervisorRefused("The controller is paused; record a recovery (runner.py recover ...) first")
        elif state.get("active"):
            raise SupervisorRefused("An unfinished issue is saved; record a recovery (runner.py recover resume) first")
        return pending

    def recovered_issue(self, pending):
        record = next(r for r in self.state.get("recoveries", []) if r["id"] == pending["id"])
        return record.get("details", {}).get("issue") or (self.state.get("active") or {}).get("issue_id")

    def announce_recovery(self, pending):
        """NEW comment on the owning issue saying which recorded recovery is being carried out."""
        record = next(r for r in self.state.get("recoveries", []) if r["id"] == pending["id"])
        details = record.get("details", {})
        active = self.state.get("active")
        issue = details.get("issue") or (active or {}).get("issue_id")
        note = None
        if details.get("note") and Path(details["note"]["path"]).is_file():
            note = Path(details["note"]["path"]).read_text()
        body = messages.recovery(self.r.ctx, record=record, step=(active or {}).get("step"), note=note,
                                 evidence_paths=[self.root / "recovery-log.jsonl"])
        self.r.emit(issue or self.config["terminal_issue"], "recovery", body, dedupe=record["id"])

    def consume(self, pending):
        kind = pending["kind"]
        self.r.allowed_phases = {"publish": set(), "review": {"review"}}.get(kind)
        if kind in ("resume", "review", "budget", "publish") and pending.get("then") == "stop":
            self.scope = "active"
        self.redeliver = bool(pending.get("redeliver"))
        for record in self.state.get("recoveries", []):
            if record["id"] == pending["id"]:
                record["consumed"] = {"launch_id": self.launch_id, "at": now()}
        self.state["pending_recovery"] = None
        self.r.save(phase="supervising", error=None)
        append_log(self.root, {"event": "consumed", "id": pending["id"], "kind": kind, "launch_id": self.launch_id,
                               "at": now()})

    # --- Scheduling ------------------------------------------------------------

    def rules(self, issue):
        if self.config["supervision"]["decision_rules"] != "honor":
            return []
        try:
            return parse_rules(issue.get("description", ""), review_criteria(issue))
        except RuleError as error:
            raise RuntimeError(f"{issue['id']}: invalid decision rules: {error}") from None

    def select(self, *, dry=False):
        """First allowlisted issue that is ready; also returns issues waiting on prerequisites."""
        waiting = {}
        done = set(self.completed())
        deferred = self.state.get("deferred", {})
        for identifier in self.config["issues"]:
            if identifier in done or identifier in deferred:
                continue
            live = self.r.linear.issue(identifier)
            if live.get("statusType") == "completed":
                self.external.add(identifier)
                continue
            self.r.verify_issue(live, dependencies=False)
            blockers = [p["id"] for p in live.get("relations", {}).get("blockedBy", [])
                        if self.r.linear.issue(p["id"]).get("statusType") != "completed"]
            if blockers:
                waiting[identifier] = blockers
                continue
            if live.get("statusType") != "unstarted":
                raise RuntimeError(f"{identifier} already claimed/canceled; reconcile ownership")
            for phase in PHASES:
                self.r.verify_model(resolve_profile(self.config, live, phase))
            self.rules(live)
            if not dry:
                self.state.setdefault("issue_cache", {})[identifier] = live
                write_json(self.root / "snapshot.json", {"at": now(), "selected": identifier, "issue": live,
                                                        "waiting": waiting})
                self.r.save()
            return identifier, waiting
        return None, waiting

    # --- Lifecycle read-back ----------------------------------------------------

    def lifecycle(self, entry):
        issue = entry["issue_id"]
        run = Path(entry["run_dir"])
        accepted = read_json(run / "final-result.json")
        contract_issue = read_json(run / "intake.json")["issue"]
        validate_review_result(accepted, contract_issue, entry["commit"])
        live = self.r.linear.issue(issue)
        self.r.verify_issue(live, completed=True)
        if not published_contract_matches(live, contract_issue):
            raise RuntimeError(f"{issue}: post-run checklist read-back mismatch")
        files = {name: sha256(run / name) for name in ("final-result.json", "intake.json", "manifest.json")
                 if (run / name).is_file()}
        integrity = run / "delivery" / "integrity.json"
        if integrity.is_file():
            files["delivery/integrity.json"] = sha256(integrity)
        record = {"at": now(), "launch_id": self.launch_id, "issue": issue, "commit": entry["commit"],
                  "run_dir": str(run), "accepted_criteria": len(accepted["acceptance"]),
                  "live": {"status": live.get("status"), "statusType": live.get("statusType"),
                           "assigneeId": live.get("assigneeId"), "projectMilestone": live.get("projectMilestone"),
                           "contract_sha256": issue_contract(live)},
                  "files_sha256": files,
                  "scope": "Controller lifecycle evidence only; not human or scientific approval."}
        directory = self.root / LIFECYCLE_DIR / issue
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "readback.json"
        if path.exists():
            path.rename(directory / f"readback-superseded-{run_id()}.json")
        write_json(path, record)
        digest = sha256(path)
        self.state.setdefault("lifecycle", {})[issue] = {"path": str(path), "sha256": digest, "synced_at": None}
        self.r.save()
        self.state["lifecycle"][issue]["synced_at"] = now()
        self.r.save()
        self.r.log(f"{issue}: lifecycle read-back verified and recorded")

    def reconcile_lifecycle(self):
        synced = self.state.get("lifecycle", {})
        for entry in self.state["history"]:
            if not synced.get(entry["issue_id"], {}).get("synced_at"):
                self.lifecycle(entry)

    # --- Issue processing -------------------------------------------------------

    def process(self, issue, *, resume=False):
        if resume and self.redeliver:
            self.r.redeliver(self.state["active"])
        try:
            self.r.work(issue, resume=resume)
        except IssueBlocked as error:
            self.on_block(issue, error)
            return "deferred"
        self.lifecycle(next(h for h in self.state["history"] if h["issue_id"] == issue))
        return "completed"

    def on_block(self, issue, error):
        active = self.state["active"]
        block = block_record(self.r, active, error, self.launch_id)
        match = evaluate(self.rules(active["issue"]), self.state["blocks"][issue])
        if match:
            rule = match["rule"]
            application = {"at": now(), "issue": issue, "rule": rule, "blocks": match["blocks"],
                           "matched_criteria": match["matched_criteria"], "launch_id": self.launch_id,
                           "contract_sha256": active["contract"]}
            self.state.setdefault("rule_applications", []).append(application)
            self.r.save()
            append_log(self.root, dict(application, event="rule_applied",
                                       authorized_by=f"decision rule {rule['text']!r} in the pinned {issue} description"))
            action = rule["then"]["action"]
            cause = {"rule": rule["id"], "rule_text": rule["text"], "block": block["id"]}
        else:
            policy = self.config["supervision"]["on_block"]
            action = "defer_issue" if policy == "continue_independent" else "stop_batch"
            cause = {"policy": f"on_block={policy}", "block": block["id"]}
        if action == "stop_batch":
            raise error
        identifier = "A-" + run_id()
        try:
            parked = park(self.r, active, identifier, allow_restore=True, keep_commit=False)
        except RuntimeError as problem:
            raise IssueBlocked(f"{error}; automatic deferral ({cause}) was not possible: {problem}",
                               error.event) from None
        defer(self.r, issue, cause=dict(cause, park=parked))
        append_log(self.root, {"event": "deferred", "issue": issue, "cause": cause, "park": parked,
                               "launch_id": self.launch_id, "at": now()})
        drafts = active.get("drafts") or {}
        who = "reviewer" if block["event"] == "review_blocked" else "worker"
        held = drafts.get("held", {}).get("review" if who == "reviewer" else "blocked")
        self.r.emit(issue, "deferred", messages.deferred(
            self.r.ctx, issue=issue, cause=cause, block=block, result=active.get("last_result"), who=who,
            draft=held["text"] if held else None, evidence_paths=[active["run_dir"], drafts.get("attempt")]),
            dedupe=block["id"])
        self.r.log(f"{issue}: deferred ({cause}); continuing with independent issues")

    # --- Terminal outcomes ------------------------------------------------------

    def extra(self, **fields):
        return dict({"launch_id": self.launch_id, "deferred": self.state.get("deferred", {}),
                     "lifecycle": self.state.get("lifecycle", {})}, **fields)

    def finish(self, outcome, reason, **fields):
        self.r.terminal(outcome, reason, self.extra(**fields))
        self.r.save(phase=outcome)
        self.r.log(f"Supervisor {outcome}: {reason}")
        return outcome

    def finish_queue(self, waiting):
        done = set(self.completed()) | self.external
        deferred = self.state.get("deferred", {})
        if not deferred and not waiting and all(i in done for i in self.config["issues"]):
            for identifier in self.config["issues"]:
                self.r.verify_issue(self.r.linear.issue(identifier), completed=True)
            self.r.finish_queue()
            self.r.log("Supervisor complete: every allowlisted issue is Done")
            return "complete"
        reason = (f"No further authorized issue is ready. Deferred: {sorted(deferred) or 'none'}; "
                  f"waiting on prerequisites: {waiting or 'none'}")
        return self.finish("partial", reason, waiting=waiting)

    # --- Main loop ----------------------------------------------------------------

    def run(self):
        r = self.r
        r.verify_config()
        r.verify_repo()
        if pid_alive(self.state.get("child_pid")):
            raise SupervisorRefused(f"Previous worker PID {self.state['child_pid']} may still be alive")
        self.check_preflight()
        if r.stop_requested():
            raise SupervisorRefused("STOP marker present; clear it (launch --clear-stop) after inspection")
        pending = self.admit()
        self.write_status("running")
        if pending:
            self.consume(pending)
        else:
            r.save(phase="supervising", error=None)
        outcome = None
        try:
            r.reconcile_events()
            if pending:
                self.announce_recovery(pending)
                r.clear_stop_marks(self.recovered_issue(pending))
            r.clear_watchdog_marks()
            self.reconcile_lifecycle()
            finished_active = False
            if self.state.get("active"):
                issue = self.state["active"]["issue_id"]
                result = self.process(issue, resume=True)
                finished_active = True
                if result == "completed" and issue in self.stop_after:
                    outcome = self.checkpoint(issue)
            while outcome is None:
                if self.scope == "active":
                    outcome = self.finish("checkpoint", "Launch scope was the active issue only",
                                          checkpoint={"scope": "active", "finished_active": finished_active})
                    break
                if r.stop_requested():
                    outcome = self.finish("stopped", "STOP marker present; stopped between issues")
                    break
                r.check_gates()
                selected, waiting = self.select()
                if selected is None:
                    outcome = self.finish_queue(waiting)
                    break
                r.allowed_phases = None
                if self.process(selected) == "completed" and selected in self.stop_after:
                    outcome = self.checkpoint(selected)
            self.write_status("exited", outcome=outcome)
            return outcome
        except (Exception, KeyboardInterrupt) as error:
            r.stop_child()
            r.log(f"Paused: {error}")
            r.report_pause(error, launch_id=self.launch_id, extra=self.extra())
            self.write_status("exited", outcome="blocked", error=str(error))
            raise

    def checkpoint(self, issue):
        self.state.setdefault("checkpoints_reached", []).append({"issue": issue, "at": now(),
                                                                 "launch_id": self.launch_id})
        (self.root / "STOP").write_text(f"Planned checkpoint after {issue} ({self.launch_id}).\n")
        return self.finish("checkpoint", f"Planned checkpoint after {issue}", checkpoint={"after": issue})


def supervise(config, linear, *, launch_id, stop_after=(), scope="queue", runner=None, install_signals=False):
    """Run the supervisor under the project lock; the STOP marker is written on exit by default."""
    root = Path(config["state_dir"])
    root.mkdir(parents=True, exist_ok=True)
    stop_on_exit = config["launcher"]["stop_on_exit"]
    with project_lock(root / "controller.lock"):
        runner = runner or Runner(config, linear)
        supervisor = Supervisor(runner, launch_id=launch_id, stop_after=stop_after, scope=scope)
        if install_signals:
            def interrupted(signum, frame):
                runner.stop_child()
                raise KeyboardInterrupt(f"Signal {signum}")
            signal.signal(signal.SIGTERM, interrupted)
        refused = False
        try:
            return supervisor.run()
        except SupervisorRefused as error:
            refused = True
            supervisor.write_status("refused", error=str(error))
            raise
        finally:
            marker = root / "STOP"
            if stop_on_exit and not refused and not marker.exists():
                marker.write_text(f"Supervisor {launch_id} exited at {now()}; inspect `status` before relaunching.\n")
