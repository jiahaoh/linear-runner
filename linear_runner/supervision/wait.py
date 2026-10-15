"""``runner.py wait``: block until a launched batch stops, then say how it stopped (W-303).

Model-free and read-only. It reads ``supervisor.json``, ``state.json``, ``terminal-report.json``
and the STOP marker in the state directory, and whether the supervisor's process still exists.
It writes nothing and takes no lock, so it runs next to the supervisor and can be started
again after every relaunch; on a batch that has already stopped it returns at once.

The supervisor has stopped when ``supervisor.json`` no longer says ``running``, or when its
process is gone (a killed unit leaves ``running`` behind). On another host than the
supervisor's the process cannot be seen; there a STOP marker counts as the stop. On the
supervisor's host a STOP marker next to a live supervisor is a requested stop (``runner.py
stop``): the supervisor exits after the current issue, and ``wait`` keeps waiting for that.

A launch runs its checks before it starts the supervisor (about 13 minutes for one project of the
§2.10 batches), and until then ``supervisor.json`` is missing or still names the launch before.
``launch`` leaves ``launching.json`` for that time; while it names a live launch whose supervisor
has not reported yet, ``wait`` and ``watch`` wait for that supervisor (W-438).

Outcomes and exit codes: ``complete`` 0 (every allowlisted issue is Done), ``checkpoint`` 3 (a
planned checkpoint or a requested stop, between issues), ``paused`` 4 (a recorded stop that
needs a recovery; the report names the issue, the step, the stop class and the reason),
``partial`` 5 (no further issue is ready: some are deferred or wait on prerequisites) and
``failed`` 1 (the supervisor refused to start or ended without recording an outcome).
"""
from __future__ import annotations

import os
from pathlib import Path
import time

from linear_runner.config import pid_alive, read_json
from linear_runner.supervision.supervisor import STATUS_NAME

POLL_SECONDS = 10
EXIT_CODES = {"complete": 0, "failed": 1, "checkpoint": 3, "paused": 4, "partial": 5}
# The supervisor's own outcome names (``supervisor.json``) and what ``wait`` reports for them.
OUTCOMES = {"complete": "complete", "checkpoint": "checkpoint", "stopped": "checkpoint", "blocked": "paused",
            "partial": "partial"}


# What ``launch`` writes while it runs: its launch id, process and host (W-438).
LAUNCHING_NAME = "launching.json"
# On another host the launch's process cannot be seen; its record counts for this long.
LAUNCHING_MAX_SECONDS = 4 * 3600


class NotLaunched(RuntimeError):
    pass


def _json(path):
    return read_json(path) if path.exists() else {}


def launching(root, clock=time.time):
    """The record of a launch that is running now and whose supervisor has not reported yet;
    ``None`` otherwise. A record whose process is gone was left by a killed launch."""
    root = Path(root)
    try:
        record = _json(root / LAUNCHING_NAME)
    except ValueError:  # being written
        return None
    if not record:
        return None
    if record.get("host") == os.uname().nodename:
        if not pid_alive(record.get("pid")):
            return None
    elif clock() - record.get("started", 0) > LAUNCHING_MAX_SECONDS:
        return None
    return None if _json(root / STATUS_NAME).get("launch_id") == record.get("launch_id") else record


def stopped(root):
    """The supervisor's status record once it has stopped; ``None`` while it runs, and while a
    launch is running its checks before it starts the supervisor."""
    path = Path(root) / STATUS_NAME
    if launching(root):
        return None
    if not path.exists():
        raise NotLaunched(f"This batch has not been launched: {path} does not exist")
    status = read_json(path)
    if status.get("status") != "running":
        return status
    if status.get("host") == os.uname().nodename:
        return None if pid_alive(status.get("pid")) else status
    return status if (Path(root) / "STOP").exists() else None


def report(root, status):
    """What ``wait`` prints: the outcome and, for a pause, what stopped and why."""
    root = Path(root)
    state = _json(root / "state.json")
    marker = root / "STOP"
    exited = status.get("status") == "exited"
    outcome = OUTCOMES.get(status.get("outcome"), "failed") if exited else "failed"
    record = {"outcome": outcome, "launch_id": status.get("launch_id"),
              "supervisor": {k: status.get(k) for k in ("status", "outcome", "updated_at")},
              "done": [h["issue_id"] for h in state.get("history", [])],
              "stop_marker": marker.read_text().strip() if marker.exists() else None}
    if outcome == "paused":
        stop = next((s for s in state.get("stops", []) if s.get("id") == status.get("stop")), {})
        record.update(issue=stop.get("issue"), step=stop.get("step"),
                      stop_class=stop.get("class") or status.get("classification"),
                      reason=stop.get("error") or status.get("error"), stop=status.get("stop"))
    elif outcome == "failed":
        record["reason"] = status.get("error") or ("the supervisor's process is gone and it recorded no outcome; "
                                                   f"see {root / 'supervisor.log'}")
    else:
        terminal = _json(root / "terminal-report.json")
        record["reason"] = terminal.get("error") if terminal.get("launch_id") == status.get("launch_id") else None
    return record


# ``watch --timeout`` ended before the batch stopped; start it again (W-346).
WATCH_TIMEOUT_EXIT = 6


def progress(root):
    """The fields ``watch`` reports a change of: active issue, phase, step, repairs, supervisor."""
    root = Path(root)
    state, status = _json(root / "state.json"), _json(root / STATUS_NAME)
    active = state.get("active") or {}
    return {"issue": active.get("issue_id"), "phase": state.get("phase"), "step": active.get("step"),
            "repairs": active.get("repairs"), "done": len(state.get("history", [])),
            "supervisor": status.get("status"), "launch_id": status.get("launch_id")}


def progress_line(fields, at):
    """One line for one change."""
    issue = fields["issue"] or "-"
    return (f"{at} {issue} phase={fields['phase']} step={fields['step'] or '-'} repairs={fields['repairs'] or 0} "
            f"done={fields['done']} supervisor={fields['supervisor']}")


def watch(root, *, out=print, sleep=time.sleep, poll_seconds=POLL_SECONDS, timeout=None, clock=time.monotonic,
          stamp=None):
    """``runner.py watch`` (W-346): print one line whenever the active issue, phase, step, repair
    count or supervisor status changes, and return ``wait``'s report once the supervisor has
    stopped (or ``None`` when ``timeout`` seconds pass first). Read-only, like ``wait``."""
    from linear_runner.engine.runner import now
    stamp = stamp or now
    started, previous, announced = clock(), None, None
    while True:
        status = stopped(root)  # raises NotLaunched before anything is printed
        launch = launching(root)
        if launch:
            # The state still shows the launch before; say once what is being waited for.
            if announced != launch.get("launch_id"):
                out(f"{stamp()} launch {launch.get('launch_id')} is running its checks; waiting for its supervisor")
                announced = launch.get("launch_id")
        else:
            fields = progress(root)
            if fields != previous:
                out(progress_line(fields, stamp()))
                previous = fields
        if status is not None:
            return report(root, status)
        if timeout is not None and clock() - started >= timeout:
            return None
        sleep(poll_seconds)


def wait(root, *, sleep=time.sleep, poll_seconds=POLL_SECONDS):
    """Block until the batch's supervisor has stopped; return ``report`` for it."""
    while True:
        status = stopped(root)
        if status is not None:
            return report(root, status)
        sleep(poll_seconds)
