"""Offline context-cost measurement from saved runner records (``runner.py measure``).

For each issue run it reports:

* intake bytes by component (issue description, other issue fields, guidance, the shared
  contract reference, context files, checks, other), plus how many unchecked criteria and
  how many bytes of Linear issue-link markup the description carries;
* for each model invocation: prompt bytes, tool-output bytes, the session's cumulative input
  after it and the input the invocation added (the same dedup and counter rules as
  ``trajectory``);
* with ``--rollouts`` (the Codex session rollout directory), the per-call context of each
  invocation: calls, first/last/max context, the part of input spent re-sending the
  starting context (``prefix``) and the part spent on context added during the invocation
  (``growth``), split by what the tool calls in between did: reading the intake packet or
  its context/contract files, running tests/checks, reading or searching files, other
  commands (scripts), or model turns without a tool call; a context drop (compaction) is
  reported separately as a negative amount.

The growth split is exact bookkeeping, not a model: with per-call context ``c_0..c_{n-1}``,
total input is ``n*c_0 + sum_j (c_{j+1}-c_j)*(n-j-1)``; each jump is charged to the tool calls
made between those two calls. Nothing here contacts Linear or Codex.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

import records
from records import parse_time

ISSUE_MARKUP = re.compile(r"<issue\b[^>]*>.*?</issue>|\[[^\]]*\]\(<?https?://[^)\s]+>?\)", re.S)
UNCHECKED = re.compile(r"^\s*[-*] \[ \] ", re.M)


def _size(value):
    return len(json.dumps(value, indent=2).encode())


def intake_components(packet, file_bytes=None):
    """Bytes per intake component; ``other`` makes the parts add up to the file size."""
    issue = packet.get("issue") or {}
    description = issue.get("description") or ""
    parts = {"issue_description": _size(description),
             "issue_other_fields": _size({k: v for k, v in issue.items() if k != "description"})}
    if packet.get("schema", "").startswith("linear-runner.intake/2"):
        parts.update(acceptance_criteria=_size(packet.get("acceptance_criteria", [])),
                     guidance=_size(packet.get("guidance", "")),
                     contract_reference=_size(packet.get("contract")),
                     context_files=_size(packet.get("context_files", [])),
                     checks=_size(packet.get("checks", [])))
    else:  # schema 1: guidance in "constraints", context file text inlined in "references"
        parts.update(guidance=_size(packet.get("constraints", "")),
                     context_files=_size(packet.get("references", {})),
                     checks=_size(packet.get("checks", [])))
    total = file_bytes if file_bytes is not None else _size(packet)
    parts["other"] = total - sum(parts.values())
    parts["total"] = total
    return parts


def description_facts(description):
    return {"description_bytes": len(description.encode()),
            "unchecked_criteria": len(UNCHECKED.findall(description)),
            "issue_link_markup_bytes": sum(len(m.group(0).encode()) for m in ISSUE_MARKUP.finditer(description))}


def read_markers(packet):
    """Strings whose appearance in a tool call means it read the intake, contract or context files."""
    markers = {"intake.json"}
    for key in (packet.get("references") or {}):
        markers.add(Path(key).name)
    for entry in packet.get("context_files") or []:
        if isinstance(entry, dict) and entry.get("path"):
            markers.add(Path(entry["path"]).name)
    contract = packet.get("contract") or {}
    if isinstance(contract, dict) and contract.get("path"):
        markers.add(Path(contract["path"]).name)
    return sorted(markers)


# --- Codex rollouts -----------------------------------------------------------------------

def default_rollouts():
    """Where Codex keeps session logs: ``$CODEX_HOME/sessions``, else ``~/.codex/sessions``."""
    home = os.environ.get("CODEX_HOME")
    return str(Path(home).expanduser() / "sessions") if home else str(Path("~/.codex/sessions").expanduser())


def rollout_location(requested=None, *, disabled=False):
    """``{"location", "found", "note"}`` for the session-log directory measure should read."""
    if disabled:
        return {"location": None, "found": False, "note": "Codex session logs not read (--no-rollouts)."}
    location = str(Path(requested or default_rollouts()).expanduser())
    if Path(location).is_dir():
        return {"location": location, "found": True, "note": None}
    return {"location": location, "found": False,
            "note": f"Codex session logs not found at {location}; per-call context growth is omitted "
                    "(pass --rollouts DIR to read them from elsewhere)."}

def find_rollout(directory, session_id):
    if not directory or not session_id:
        return None
    matches = sorted(Path(directory).expanduser().rglob(f"rollout-*-{session_id}.jsonl"))
    return matches[-1] if matches else None


def _tool_text(payload):
    kind = payload.get("type")
    if kind == "custom_tool_call":
        return "call", str(payload.get("input", ""))
    if kind == "function_call":
        return "call", str(payload.get("arguments", ""))
    if kind == "local_shell_call":
        return "call", json.dumps(payload.get("action", {}))
    if kind in ("custom_tool_call_output", "function_call_output"):
        output = payload.get("output")
        if isinstance(output, list):
            return "output", "".join(str(o.get("text", "")) for o in output if isinstance(o, dict))
        return "output", str(output or "")
    return None, None


def rollout_calls(path):
    """[(timestamp, context tokens, [tool call texts before the next call], output bytes)]."""
    calls, pending_calls, pending_output = [], [], 0
    for line in Path(path).read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        payload = event.get("payload") or {}
        if payload.get("type") == "token_count" and isinstance(payload.get("info"), dict):
            last = payload["info"].get("last_token_usage") or {}
            if isinstance(last.get("input_tokens"), int):
                if calls:
                    calls[-1]["after"] = pending_calls
                    calls[-1]["after_output_bytes"] = pending_output
                calls.append({"at": parse_time(event.get("timestamp")), "context": last["input_tokens"],
                              "after": [], "after_output_bytes": 0})
                pending_calls, pending_output = [], 0
            continue
        role, text = _tool_text(payload)
        if role == "call":
            pending_calls.append(text)
        elif role == "output":
            pending_output += len(text.encode())
    if calls:
        calls[-1]["after"], calls[-1]["after_output_bytes"] = pending_calls, pending_output
    return calls


GROWTH_KINDS = ("intake_or_context_reads", "test_and_check_runs", "file_reads_and_search", "other_commands",
                "model_turns_without_tools", "context_compaction")
_TESTS = re.compile(r"\b(pytest|unittest|sphinx-build|check_reference|ruff|mypy|tox|nox)\b")
_SCRIPT = re.compile(r"\bpython3?(?:\.\d+)?\s+(?:-\s*<<|-c\b|\S+\.py\b)|<<\s*'?(?:PY|EOF)")
_READS = re.compile(r"(?:^|[\s\"'(;&|])(cat|sed|head|tail|nl|less|rg|grep|find|ls|jq|wc|diff|git (?:show|diff|log|status))\b")


def classify(texts, markers):
    """What the tool calls between two model calls did (first matching kind wins)."""
    if not texts:
        return "model_turns_without_tools"
    joined = "\n".join(texts)
    if any(marker in joined for marker in markers):
        return "intake_or_context_reads"
    if _TESTS.search(joined):
        return "test_and_check_runs"
    if _READS.search(joined) and not _SCRIPT.search(joined):
        return "file_reads_and_search"
    return "other_commands"


def context_growth(calls, markers):
    """Split one invocation's input into prefix and growth by source (see module docstring)."""
    n = len(calls)
    if not n:
        return None
    contexts = [c["context"] for c in calls]
    split = {kind: 0 for kind in GROWTH_KINDS}
    output = {kind: 0 for kind in GROWTH_KINDS}
    for j in range(n - 1):
        jump = contexts[j + 1] - contexts[j]
        kind = "context_compaction" if jump < 0 else classify(calls[j]["after"], markers)
        split[kind] += jump * (n - j - 1)
        output[kind] += calls[j]["after_output_bytes"]
    return {"calls": n, "first_context": contexts[0], "last_context": contexts[-1], "max_context": max(contexts),
            "input": sum(contexts), "prefix": n * contexts[0], "growth": split, "tool_output_bytes": output}


# --- Measurement ----------------------------------------------------------------------------

def measure(roots, *, issues=None, rollouts=None, compact=None, rollout_status=None):
    """Per issue: intake components and per-invocation usage/growth. ``compact`` is an optional
    ``callable(packet) -> packet`` replaying a new intake builder on each saved intake."""
    invocations = [i for i in records.find_invocations(roots) if not issues or i["issue"] in issues]
    intakes = {(e["issue"], e["run_id"]): e for e in records.find_intakes(roots)
               if not issues or e["issue"] in issues}
    report = {"schema": "linear-runner.measure/1", "issues": {},
              "rollouts": rollout_status or {"location": str(rollouts) if rollouts else None,
                                             "found": bool(rollouts and Path(rollouts).expanduser().is_dir()),
                                             "note": None if rollouts else "No Codex session-log directory given."}}
    previous = {}
    for key, entry in sorted(intakes.items()):
        packet = json.loads(Path(entry["path"]).read_text())
        size = Path(entry["path"]).stat().st_size
        issue = report["issues"].setdefault(entry["issue"], {"intakes": [], "invocations": []})
        recorded = Path(entry["path"]).with_name("intake-components.json")
        record = {"run_id": entry["run_id"], "path": str(entry["path"]), "versions": entry["versions"],
                  "components": intake_components(packet, size),
                  "recorded_components": records.read_json(recorded) if recorded.is_file() else None,
                  **description_facts((packet.get("issue") or {}).get("description") or "")}
        if compact is not None:
            replay = compact(packet)
            encoded = json.dumps(replay, indent=2) + "\n"
            record["compact_replay"] = intake_components(replay, len(encoded.encode()))
        issue["intakes"].append(record)
    for item in invocations:
        issue = report["issues"].setdefault(item["issue"], {"intakes": [], "invocations": []})
        counter = item["counter"] or {}
        prior = previous.get(item["session_id"]) if item["session_id"] else None
        row = {"attempt": item["attempt"], "phase": item["phase"], "session_id": item["session_id"],
               "start": item["start"], "finished": bool(item["end"]), "prompt_bytes": item["prompt_bytes"],
               "tool_output_bytes": item["tool_output_bytes"],
               "compact_token_limit": item.get("compact_token_limit"),
               "session_input_after": counter.get("input_tokens"),
               "input_added": (counter["input_tokens"] - (prior or 0)) if "input_tokens" in counter else None,
               "output_added": (counter["output_tokens"] - (previous.get(("out", item["session_id"])) or 0))
               if "output_tokens" in counter else None}
        if "input_tokens" in counter and item["session_id"]:
            previous[item["session_id"]] = counter["input_tokens"]
            previous[("out", item["session_id"])] = counter.get("output_tokens", 0)
        if item["prompt_bytes"] is None:
            prompt = Path(item["source"]).parent / "prompt.txt"
            row["prompt_bytes"] = prompt.stat().st_size if prompt.is_file() else None
        path = find_rollout(rollouts, item["session_id"])
        if path is not None:
            start, end = parse_time(item["start"]), parse_time(item["end"])
            calls = [c for c in rollout_calls(path) if c["at"] and c["at"] >= start and (end is None or c["at"] <= end)]
            intake = next((json.loads(Path(e["path"]).read_text()) for k, e in intakes.items()
                           if k == (item["issue"], item["run_id"])), {})
            growth = context_growth(calls, read_markers(intake))
            if growth:
                row["rollout"] = dict(growth, path=str(path))
        issue["invocations"].append(row)
    for check in records.find_checks(roots):
        if issues and check["issue"] not in issues:
            continue
        issue = report["issues"].setdefault(check["issue"], {"intakes": [], "invocations": []})
        issue.setdefault("checks", []).append({k: check[k] for k in ("run_id", "stage", "name", "exit_code", "status",
                                                                     "reused")})
    report["totals"] = totals(report)
    return report


def totals(report):
    """Batch-level sums: intake components, invocation input and the growth split."""
    components, replay = {}, {}
    rollout = dict({"input": 0, "prefix": 0, "invocations": 0}, **{kind: 0 for kind in GROWTH_KINDS})
    added = {}
    for issue in report["issues"].values():
        for intake in issue["intakes"]:
            for key, value in intake["components"].items():
                components[key] = components.get(key, 0) + value
            for key, value in (intake.get("compact_replay") or {}).items():
                replay[key] = replay.get(key, 0) + value
        for row in issue["invocations"]:
            if row["input_added"] is not None:
                added[row["phase"]] = added.get(row["phase"], 0) + row["input_added"]
            growth = row.get("rollout")
            if growth and growth.get("calls"):
                rollout["invocations"] += 1
                rollout["input"] += growth["input"]
                rollout["prefix"] += growth["prefix"]
                for key in GROWTH_KINDS:
                    rollout[key] += growth["growth"][key]
    return {"intake_components": components, "compact_replay": replay or None, "input_added_by_phase": added,
            "rollout": rollout if rollout["invocations"] else None}


def render_markdown(report):
    status = report.get("rollouts") or {}
    logs = (f"Codex session logs: {status['location']}." if status.get("found")
            else status.get("note") or "Codex session logs were not read.")
    lines = ["# Context-cost measurement", "", "Offline, from saved runner records; no model was used. " + logs, "",
             "## Intake packets", "",
             "| Issue | Run | Total | Description | Other issue fields | Guidance | Context files | Checks | Other "
             "| Criteria | Link markup | Compact replay |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, issue in report["issues"].items():
        for intake in issue["intakes"]:
            c = intake["components"]
            replay = intake.get("compact_replay")
            cells = [name, intake["run_id"], c["total"], c["issue_description"], c["issue_other_fields"],
                     c.get("guidance", 0), c.get("context_files", 0), c.get("checks", 0), c["other"],
                     intake["unchecked_criteria"], intake["issue_link_markup_bytes"],
                     replay["total"] if replay else "n/a"]
            lines.append("| " + " | ".join(f"{v:,}" if isinstance(v, int) else str(v) for v in cells) + " |")
    lines += ["", "## Invocations", "",
              "| Issue | Attempt | Phase | Compact limit | Prompt bytes | Tool output bytes | Session input after "
              "| Input added | Calls "
              "| First ctx | Last ctx | Prefix | Growth: intake/context reads | Growth: tests/checks "
              "| Growth: file reads/search | Growth: other commands | Growth: no-tool turns | Compaction |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]

    def fmt(value):
        return "unknown" if value is None else f"{value:,}" if isinstance(value, int) else str(value)
    for name, issue in report["issues"].items():
        for row in issue["invocations"]:
            r = row.get("rollout") or {}
            growth = r.get("growth") or {}
            lines.append("| " + " | ".join([name, row["attempt"], row["phase"],
                                             fmt(row.get("compact_token_limit")) if row.get("compact_token_limit")
                                             else "default", fmt(row["prompt_bytes"]),
                                             fmt(row["tool_output_bytes"]), fmt(row["session_input_after"]),
                                             fmt(row["input_added"]), fmt(r.get("calls")), fmt(r.get("first_context")),
                                             fmt(r.get("last_context")), fmt(r.get("prefix")),
                                             *(fmt(growth.get(kind)) for kind in GROWTH_KINDS)]) + " |")
    lines += ["", "## Check outcomes", "",
              "Recorded validation and delivery checks; an allowed empty selection counts as passing.", "",
              "| Issue | Run | Stage | Check | Exit | Outcome | Reused |", "|---|---|---|---|---|---|---|"]
    for name, issue in report["issues"].items():
        for check in issue.get("checks", []):
            lines.append("| " + " | ".join([name, check["run_id"], check["stage"], str(check["name"]),
                                             fmt(check["exit_code"]), fmt(records.outcome_text(check["status"])),
                                             "yes" if check["reused"] else "no"]) + " |")
    t = report["totals"]
    lines += ["", "## Totals", "", "```json", json.dumps(t, indent=2), "```", ""]
    return "\n".join(lines)
