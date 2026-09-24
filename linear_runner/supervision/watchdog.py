"""Model-free watchdog: ``runner.py watchdog --batch ...``, run by a host timer.

It alerts (one NEW comment on the owning issue, or the terminal issue when no issue is
active, plus one notifier call) when:

* ``gone``: ``supervisor.json`` still says ``running`` but that process no longer exists,
  so the supervisor ended without writing a terminal outcome (killed, out of memory,
  host problem);
* ``stalled``: the supervisor process exists but neither ``state.json``,
  ``supervisor.json`` nor the active issue's run directory has changed for
  ``attention.watchdog.stall_minutes`` (120).

``launch`` (systemd backend) starts a user timer that runs this every
``attention.watchdog.interval_minutes`` (10) with ``--launch-id`` and ``--timer``. The
watchdog then stops its own timer once that launch's supervisor has an outcome (or has
vanished) and any alert is posted, or when a newer launch took over.

Each condition alerts once. Alerts are recorded in ``<state dir>/watchdog.json`` (a stall
is keyed by the time of the last progress, so a new stall after progress alerts again).
The watchdog never takes the project lock, never writes ``state.json`` and never starts
a model. It applies the needs-input mechanism to the issue it alerts on; the next
successful launch removes it.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import subprocess
import time

from linear_runner.linear import attention
from linear_runner.config import read_json, write_json
from linear_runner.linear import messages
from linear_runner.linear import updates

LEDGER_NAME = "watchdog.json"


def pid_alive(pid):
    return bool(pid) and Path(f"/proc/{pid}").exists()


def _epoch(value):
    try:
        return dt.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _load(root):
    path = Path(root) / LEDGER_NAME
    return read_json(path) if path.exists() else {"alerts": {}}


def timer_stopped(root, timer):
    return bool(_load(root).get("timers", {}).get(timer, {}).get("stopped_at"))


def _note_stop(record, timer, reason, result):
    record.setdefault("timers", {})[timer] = {"stopped_at": _iso(time.time()), "reason": reason, "result": result}


def record_timer_stop(root, timer, reason, result):
    record = _load(root)
    _note_stop(record, timer, reason, result)
    write_json(Path(root) / LEDGER_NAME, record)


def systemctl_stop(timer, run=subprocess.run):
    try:
        process = run(["systemctl", "--user", "stop", timer], capture_output=True, text=True)
        return {"exit_code": process.returncode, "stderr": (process.stderr or "").strip()}
    except OSError as error:
        return {"error": str(error)}


def last_progress(root, state, status):
    """Latest recorded progress: state, supervisor status, or a file in the active run directory."""
    times = [t for t in (_epoch(state.get("updated_at")), _epoch(status.get("updated_at"))) if t]
    active = state.get("active")
    run_dir = Path(active["run_dir"]) if active and active.get("run_dir") else None
    if run_dir and run_dir.is_dir():
        for path in [*run_dir.glob("*"), *run_dir.glob("*/*"), *run_dir.glob("*/outbox/*")]:
            try:
                if path.is_file():
                    times.append(path.stat().st_mtime)
            except OSError:
                continue
    return max(times) if times else None


def last_update(state, issue):
    """First sentence of the latest comment posted for ``issue``, else the worker's summary."""
    posted = [e for e in state.get("events", {}).values() if e["issue"] == issue and e["status"] == "posted"]
    if posted:
        latest = max(posted, key=lambda e: e.get("recorded_at") or "")
        return f"From the {latest['kind']} comment: {updates.first_sentence(latest['body'])}"
    summary = ((state.get("active") or {}).get("last_result") or {}).get("summary")
    return updates.first_sentence(summary) if summary else ""


def check(config, linear, *, clock=time.time, alive=pid_alive, hostname=None, notify_run=subprocess.run, log=print,
          launch_id=None, timer=None, systemctl=subprocess.run):
    """One watchdog pass. ``launch_id``/``timer`` are given by the launch-started timer."""
    root = Path(config["state_dir"])
    status_path = root / "supervisor.json"
    if not status_path.exists():
        return {"status": "idle", "reason": "no supervisor has been launched for this batch"}
    status = read_json(status_path)
    ledger_path = root / LEDGER_NAME
    record = read_json(ledger_path) if ledger_path.exists() else {"alerts": {}}
    record.setdefault("alerts", {})

    def save():
        write_json(ledger_path, record)

    ledger = updates.Ledger(lambda: record, save, linear, config["batch_id"], log)
    try:
        ledger.reconcile()  # an alert whose post failed earlier is retried, never duplicated
    except Exception as error:
        log(f"Watchdog: earlier alert still not posted: {error}")
    def stop_own_timer(reason):
        if timer and not record.get("timers", {}).get(timer, {}).get("stopped_at"):
            _note_stop(record, timer, reason, systemctl_stop(timer, systemctl))
            save()
            return True
        return False

    if launch_id and status.get("launch_id") != launch_id:
        stopped = stop_own_timer(f"launch {status.get('launch_id')} replaced launch {launch_id}")
        return {"status": "superseded", "timer_stopped": stopped}
    if status.get("status") != "running":
        reason = f"supervisor {status.get('launch_id')} is {status.get('status')}"
        stopped = False if ledger.pending() else stop_own_timer(reason + " and every alert is posted")
        return {"status": "ok", "reason": reason, "timer_stopped": stopped}
    if status.get("host") and status["host"] != (hostname or os.uname().nodename):
        return {"status": "skipped", "reason": f"the supervisor runs on {status['host']}; run the watchdog there"}
    state = read_json(root / "state.json") if (root / "state.json").exists() else {}
    active = state.get("active")
    issue = active["issue_id"] if active else None
    subject = issue or f"batch {config['batch_id']}"
    progress = last_progress(root, state, status)
    now = clock()
    launch = status.get("launch_id")
    if not alive(status.get("pid")):
        condition, key, minutes = "gone", f"gone:{launch}", None
        observed = (f"supervisor.json still says running (launch {launch}, PID {status.get('pid')} on "
                    f"{status.get('host')}), but that process no longer exists and no terminal outcome was written.")
        if progress:
            observed += f" The last recorded progress was at {_iso(progress)}."
    else:
        stall = config["attention"]["watchdog"]["stall_minutes"]
        if progress is None or now - progress < stall * 60:
            return {"status": "ok", "idle_minutes": None if progress is None else int((now - progress) // 60)}
        condition, key, minutes = "stalled", f"stalled:{launch}:{int(progress)}", int((now - progress) // 60)
        observed = (f"The supervisor (launch {launch}, PID {status.get('pid')}) is running, but nothing in the state "
                    f"or the active run directory has changed since {_iso(progress)}.")
    if key in record["alerts"]:
        return {"status": "already-alerted", "key": key, "condition": condition}
    target = issue or config["terminal_issue"]
    body = messages.watchdog(messages.context(config), condition=condition, subject=subject, minutes=minutes,
                             observed=observed, last_update=last_update(state, issue) if issue else "",
                             evidence_paths=[status_path, root / "supervisor.log", (active or {}).get("run_dir")])
    alert = {"key": key, "condition": condition, "at": _iso(now), "launch_id": launch, "issue": target,
             "notified": None, "comment": None}
    record["alerts"][key] = alert
    save()
    alert["notified"] = attention.notify(config["attention"]["notifier"], messages.subject_line(body), body,
                                         run=notify_run)
    save()
    try:
        alert["comment"] = ledger.emit(target, "watchdog", body, dedupe=key, now=_iso(now))["key"]
    except Exception as error:
        alert["post_error"] = str(error)
    try:  # removed again when the next launch starts successfully
        mark = attention.mark_needs_input(linear, target, config["attention"]["needs_input"])
        record.setdefault("needs_input", {})[target] = mark
    except Exception as error:
        alert["needs_input_error"] = str(error)
    save()
    stopped = condition == "gone" and bool(alert["comment"]) and stop_own_timer(
        "the supervisor is gone and the alert is posted; the next launch starts a new timer")
    return {"status": "alerted", "key": key, "condition": condition, "issue": target,
            "posted": bool(alert["comment"]), "notified": alert["notified"].get("sent"), "timer_stopped": stopped}
