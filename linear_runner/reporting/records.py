"""Read saved runner records offline: sessions, checks and intake packets.

Runner artifacts live at ``<artifact root>/<issue>/<run id>/<phase>-<run id>/session.json``
(plus ``validation-*/checks.json``, ``delivery/checks.json`` and ``intake.json`` in the
issue run directory). Recoveries and continuations often copy whole run directories
into new evidence roots, so the same record can appear several times. This module finds
every copy under the given roots and keeps one record per identity:

* an invocation is identified by its session ID, start time and role (worker/reviewer);
* a check record by its issue, run, validation directory, name and start time.

Usage semantics (the same as ``runner.usage_totals``): counters are cumulative within a
session (Codex reports them so; the runner accumulates Claude's per-invocation counters), so the latest observed counter of each unique session counts once; cached input
is a subset of input and reasoning output a subset of output; a missing counter is unknown,
never zero. An invocation's delta is exact unless an earlier invocation of its session,
after the session's latest counter, recorded none (a failed turn): then it is an upper
bound (``delta_basis``). No model, network or Linear access.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import re

RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
ATTEMPT = re.compile(r"^(implement|repair|review)-(\d{8}T\d{6}Z-[0-9a-f]{8})$")
ROLE = {"implement": "worker", "repair": "worker", "review": "reviewer"}
LEGACY_PHASE = {"worker": "implement", "reviewer": "review"}
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
RSS = re.compile(r"Maximum resident set size \(kbytes\):\s*(\d+)")


def parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def locate(path):
    """``(issue, run id, run dir)`` for a path below ``<issue>/<run id>/``, else ``None``."""
    parts = Path(path).parts
    for index, part in enumerate(parts):
        if index and RUN_ID.match(part):
            return parts[index - 1], part, Path(*parts[:index + 1])
    return None


def _walk(roots, name):
    seen = set()
    for root in roots:
        root = Path(root)
        candidates = [root] if root.is_file() else sorted(root.rglob(name)) if root.is_dir() else []
        for path in candidates:
            if path.name == name and path.is_file() and path.resolve() not in seen:
                seen.add(path.resolve())
                yield path


# --- Invocations ------------------------------------------------------------------

def counter_of(meta):
    """The latest cumulative usage counter of one invocation (``None`` when not emitted)."""
    events = ((meta.get("execution_evidence") or {}).get("usage_events")) or []
    usage = [e.get("usage") for e in events if isinstance(e, dict) and isinstance(e.get("usage"), dict)]
    if not usage:
        return None
    return {k: v for k, v in usage[-1].items() if isinstance(v, int) and not isinstance(v, bool)}


UPPER_BOUND = "cumulative-upper-bound"


def delta_basis(item, gap):
    """How exact an invocation's usage delta is: ``unavailable`` (no counter), ``delta`` or
    ``cumulative-upper-bound`` when ``gap`` (an invocation of the same session without a
    counter came after the session's latest counter). Per-invocation counters (Claude) stay
    exact: the runner added only known counters to them."""
    if item.get("counter") is None:
        return "unavailable"
    return UPPER_BOUND if gap and not item.get("invocation_scoped") else "delta"


def _count(value):
    """A recorded count, or ``None`` when it was not recorded (never 0 for missing)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read_invocation(path):
    meta = read_json(path)
    located = locate(path)
    if not isinstance(meta, dict) or not located or not parse_time(meta.get("started_at")):
        return None
    issue, run, run_dir = located
    match = ATTEMPT.match(path.parent.name)
    phase = match.group(1) if match else meta.get("phase") or LEGACY_PHASE.get(meta.get("role"))
    if phase not in ROLE:
        return None
    result = read_json(path.parent / "phase-result.json")
    selection = meta.get("selection") or {}
    return {
        "issue": issue, "run_id": run, "attempt": path.parent.name, "phase": phase, "role": ROLE[phase],
        "session_id": meta.get("session_id"), "backend": meta.get("backend") or "codex",
        "start": meta["started_at"], "end": meta.get("finished_at"),
        "wall_seconds": meta.get("wall_seconds") if meta.get("finished_at") else None,
        "exit_code": meta.get("exit_code"),
        "status": result.get("status") if isinstance(result, dict) else None,
        "requested_model": meta.get("requested_model") or selection.get("model"),
        "requested_effort": meta.get("requested_reasoning_effort") or selection.get("effort"),
        "profile": selection.get("profile"),
        "selection_source": selection.get("selection_source"),
        "observed_models": (meta.get("execution_evidence") or {}).get("observed_models"),
        "prompt_bytes": meta.get("prompt_bytes"),
        "tool_output_bytes": meta.get("tool_output_bytes"),
        "handoff": meta.get("handoff"),
        "compact_token_limit": meta.get("compact_token_limit"),
        "counter": counter_of(meta),
        "invocation_scoped": bool((meta.get("execution_evidence") or {}).get("invocation_usage_events")),
        "tool_calls": _count((meta.get("execution_evidence") or {}).get("completed_tool_calls")),
        "failed_tool_calls": _count((meta.get("execution_evidence") or {}).get("failed_tool_calls")),
        "source": str(path), "sha256": sha256(path), "copies": [],
    }


def invocation_key(item):
    return (item["session_id"], item["start"], item["role"])


def find_invocations(roots):
    """Unique invocations under ``roots`` (sorted by start); copies are listed, not counted."""
    unique = {}
    for path in _walk(roots, "session.json"):
        item = read_invocation(path)
        if item is None:
            continue
        key = invocation_key(item)
        if key in unique:
            kept = unique[key]
            # Prefer the most complete copy (a later copy may carry the finished record).
            if (item["end"] and not kept["end"]) or (item["counter"] and not kept["counter"]):
                item["copies"] = kept["copies"] + [kept["source"]]
                unique[key] = item
            else:
                kept["copies"].append(str(path))
        else:
            unique[key] = item
    return sorted(unique.values(), key=lambda i: (parse_time(i["start"]), i["role"]))


# --- Checks -----------------------------------------------------------------------------

def _resolve_log(record_log, checks_path, run_dir):
    """The check log: the recorded path, else the same path relative to this copy's run dir."""
    if not isinstance(record_log, str) or not record_log:
        return None
    path = Path(record_log)
    if path.is_file():
        return path
    parts = path.parts
    for index, part in enumerate(parts):
        if RUN_ID.match(part):
            candidate = Path(run_dir, *parts[index + 1:])
            if candidate.is_file():
                return candidate
    candidate = checks_path.parent / path.name
    return candidate if candidate.is_file() else None


def check_status(record):
    """``passed``, ``failed`` or ``empty`` (an allowed empty selection); older records have no
    ``status`` field and are derived from the exit code."""
    if record.get("status") in ("passed", "failed", "empty"):
        return record["status"]
    code = record.get("exit_code")
    return None if code is None else "passed" if code == 0 else "failed"


def outcome_text(status):
    """A check outcome for people: an allowed empty selection is named as such."""
    return "empty (no tests selected; allowed)" if status == "empty" else status


def find_checks(roots):
    """Unique runner check records from ``validation-*/checks.json`` and ``delivery/checks.json``."""
    unique = {}
    for path in _walk(roots, "checks.json"):
        located = locate(path)
        if not located or path.parent.parent != located[2]:
            continue  # only the run-level summaries, not per-check subdirectories
        stage = path.parent.name
        if not (stage.startswith("validation-") or stage == "delivery"):
            continue
        records = read_json(path)
        if not isinstance(records, list):
            continue
        issue, run, run_dir = located
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            name = record.get("name") or ("delivery-" + str(index) if stage == "delivery" else "check-" + str(index))
            key = (issue, run, stage, name, record.get("started_at"))
            if key in unique:
                unique[key]["copies"].append(str(path))
                continue
            start, end = parse_time(record.get("started_at")), parse_time(record.get("finished_at"))
            reused = bool(record.get("reused"))
            log = _resolve_log(record.get("log"), path, run_dir)
            recorded_hash = record.get("sha256")
            rss = None
            if log is not None:
                match = RSS.search(log.read_bytes().decode("utf-8", "replace"))
                rss = int(match.group(1)) if match else None
            unique[key] = {
                "issue": issue, "run_id": run, "stage": stage, "name": name,
                "exit_code": record.get("exit_code"), "status": check_status(record), "reused": reused,
                "started_at": record.get("started_at"), "finished_at": record.get("finished_at"),
                "seconds": (end - start).total_seconds() if start and end and not reused else None,
                "rss_kib": rss, "log": str(log) if log else record.get("log"),
                "log_found": log is not None,
                "hash_matches": (sha256(log) == recorded_hash) if log is not None and recorded_hash else None,
                "source": str(path), "copies": [],
            }
    return sorted(unique.values(), key=lambda c: (c["issue"], c["run_id"], c["stage"], c["started_at"] or ""))


# --- Intake packets ----------------------------------------------------------------------

def find_intakes(roots):
    """The latest copy of each run's ``intake.json`` (by modification time), with version count."""
    latest = {}
    for path in _walk(roots, "intake.json"):
        located = locate(path)
        if not located or path.parent != located[2]:
            continue
        key = located[:2]
        entry = latest.setdefault(key, {"issue": located[0], "run_id": located[1], "paths": [], "hashes": set()})
        entry["paths"].append(path)
        entry["hashes"].add(sha256(path))
    for entry in latest.values():
        entry["path"] = max(entry["paths"], key=lambda p: p.stat().st_mtime)
        entry["versions"] = len(entry.pop("hashes"))
        entry["copies"] = len(entry.pop("paths"))
    return sorted(latest.values(), key=lambda e: (e["issue"], e["run_id"]))
