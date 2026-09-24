"""Getting a person's attention when the batch stops: classification, needs-input, notifier.

* ``classify_stop`` sorts every stop into one of four classes (rule below, tested).
* ``mark_needs_input``/``clear_needs_input`` apply the configured needs-input mechanism
  to the owning issue: a label, a named workflow state, or the owner mention alone.
* ``notify`` is the out-of-band notifier, called once per stop and per watchdog alert.

The mechanism, label/state names, notifier backend and command, and the owner mention
handle are DRAFT settings (workspace and site ``attention`` blocks).
"""
from __future__ import annotations

import subprocess

from linear_runner.backends import failure_patterns

STOP_CLASSES = ("runner-defect", "environment", "technical-block", "needs-decision")
# Issue-level blocks (IssueBlocked.event) and their class.
EVENT_CLASSES = {"worker_blocked": "needs-decision", "review_blocked": "needs-decision",
                 "budget_exceeded": "needs-decision", "checks_failed": "technical-block",
                 "delivery_failed": "technical-block"}
# Message fragments of batch-level RuntimeErrors raised by the runner, Linear client or a
# model backend ("<label> failed"/"<label> exceeded" for every backend, e.g. Codex, Claude).
# Environment patterns are checked first.
ENVIRONMENT_PATTERNS = ("Linear HTTP", "Linear OAuth", "credential", "Claude authentication", "Linear MCP", "MCP redirect", "MCP stream",
                        "MCP protocol", "returned unexpected", "Cannot reconcile paginated", "Cannot read paginated",
                        *failure_patterns(), "Missing structured result", "unavailable in host CLI catalog",
                        "CLI version", "CLI unavailable", "Another controller", "may still be alive", "read-back",
                        "Signal ")
DECISION_PATTERNS = ("changed outside", "scope", "Human approval", "is not Done", "Configuration/guidance changed",
                     "already claimed", "branch moved", "changed Git history", "Frozen validated source",
                     "differs from authorized intake", "requires a project milestone", "Incomplete prerequisite",
                     "requires a clean worktree", "interrupted; inspect", "Validation modified source",
                     "Source changed after validation", "Duplicate", "decision rules", "STOP marker",
                     "model pool", "model: label", "model_overrides")
MECHANISMS = ("label", "state", "mention")
NOTIFIER_BACKENDS = ("none", "command", "linear-mention-only")


def classify_stop(error):
    """Return one of STOP_CLASSES for an exception that stopped the batch.

    1. An issue-level block (an exception with a known ``event``) uses EVENT_CLASSES:
       blocked worker or review and soft budget need a decision; failing checks or
       delivery are a technical block.
    2. A signal (KeyboardInterrupt), OSError, TimeoutError or subprocess error is
       ``environment``.
    3. A RuntimeError whose message contains an ENVIRONMENT_PATTERNS fragment is
       ``environment``; one with a DECISION_PATTERNS fragment is ``needs-decision``
       (something changed outside the runner); any other RuntimeError is
       ``technical-block``.
    4. Anything else (KeyError, TypeError, AssertionError, ...) is a ``runner-defect``.
    """
    event = getattr(error, "event", None)
    if event in EVENT_CLASSES:
        return EVENT_CLASSES[event]
    if isinstance(error, (KeyboardInterrupt, OSError, TimeoutError, subprocess.SubprocessError)):
        return "environment"
    if isinstance(error, RuntimeError):
        text = str(error)
        if any(p in text for p in ENVIRONMENT_PATTERNS):
            return "environment"
        if any(p in text for p in DECISION_PATTERNS):
            return "needs-decision"
        return "technical-block"
    return "runner-defect"


# --- Needs-input mechanisms ---------------------------------------------------------

def label_names(issue):
    return [v.get("name") if isinstance(v, dict) else v for v in issue.get("labels", []) or []]


def mark_needs_input(linear, issue, settings):
    """Apply the configured mechanism to ``issue``; return a record (raises on read-back failure)."""
    mechanism = settings["mechanism"]
    if mechanism == "mention":
        return {"mechanism": "mention", "issue": issue, "applied": False}
    live = linear.issue(issue)
    if mechanism == "label":
        name = settings["label"]
        labels = label_names(live)
        if name not in labels:
            linear.call("save_issue", id=issue, labels=labels + [name])
        if name not in label_names(linear.issue(issue)):
            raise RuntimeError(f"Linear read-back does not show the {name!r} label on {issue}")
        return {"mechanism": "label", "issue": issue, "label": name, "applied": True}
    if mechanism == "state":
        name = settings["state"]
        previous = live.get("status")
        if previous != name:
            linear.call("save_issue", id=issue, state=name)
        if linear.issue(issue).get("status") != name:
            raise RuntimeError(f"Linear read-back does not show {issue} in state {name!r}")
        return {"mechanism": "state", "issue": issue, "state": name, "previous_status": previous, "applied": True}
    raise ValueError(f"Unknown needs-input mechanism {mechanism!r}")


def clear_needs_input(linear, mark):
    """Undo a mark from ``mark_needs_input`` (label removed, previous state restored)."""
    issue = mark["issue"]
    if not mark.get("applied"):
        return {"issue": issue, "cleared": False}
    live = linear.issue(issue)
    if mark["mechanism"] == "label":
        labels = label_names(live)
        if mark["label"] in labels:
            linear.call("save_issue", id=issue, labels=[n for n in labels if n != mark["label"]])
        if mark["label"] in label_names(linear.issue(issue)):
            raise RuntimeError(f"Linear read-back still shows the {mark['label']!r} label on {issue}")
    elif mark["mechanism"] == "state" and live.get("status") == mark["state"] and mark.get("previous_status"):
        linear.call("save_issue", id=issue, state=mark["previous_status"])
        if linear.issue(issue).get("status") != mark["previous_status"]:
            raise RuntimeError(f"Linear read-back does not show {issue} back in {mark['previous_status']!r}")
    return {"issue": issue, "cleared": True}


# --- Out-of-band notifier -------------------------------------------------------------

def notify(settings, subject, message, *, run=subprocess.run):
    """Send one notification; never raises. ``command`` gets the message on stdin and
    ``{subject}`` replaced in its argv (no shell)."""
    backend = settings.get("backend", "none")
    record = {"backend": backend, "subject": subject, "sent": False}
    if backend == "none":
        return record
    if backend == "linear-mention-only":
        record["note"] = "no out-of-band message; the Linear comment mentions the owner"
        return record
    if backend != "command" or not settings.get("command"):
        record["error"] = f"notifier backend {backend!r} is not usable"
        return record
    argv = [part.replace("{subject}", subject) for part in settings["command"]]
    try:
        process = run(argv, input=message, text=True, capture_output=True, timeout=settings.get("timeout_seconds", 30))
    except (OSError, subprocess.SubprocessError) as error:
        record["error"] = str(error)
        return record
    record.update(argv=argv, exit_code=process.returncode, sent=process.returncode == 0,
                  stderr=(process.stderr or "")[-500:])
    return record
