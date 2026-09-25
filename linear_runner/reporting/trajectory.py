"""Deterministic trajectory and usage report from saved runner records (no model).

``build`` turns the records found by ``records`` into per-issue summaries, one row per
invocation (attempt), one row per session, a validation audit and a batch comparison.
``render_markdown``/``render_html`` show the same data for people; ``write`` stores the
JSON next to them. ``compare_recorded`` checks the result against an earlier recorded
trajectory/comparison so a reproduction is a test, not a claim.

Semantics:

* invocations are deduplicated by session ID + start + role; copies are listed;
* the latest observed cumulative counter of each unique session counts once, and an
  invocation's delta subtracts the session's previous counter; after an invocation of the
  same session without a counter (a failed turn) the delta is an upper bound
  (``usage_basis`` ``cumulative-upper-bound``, shown as "≤ N"), never an exact delta;
* cached input is a subset of input and reasoning a subset of output (never added);
* a session without a counter makes the issue's ``usage`` unknown (``null``), never zero;
  ``totals`` (what the tables show, and what the runner's run summary comments show) sum
  what was reported and mark the result: a total is exact only when every attempt in it
  reported the figure, otherwise it is a lower bound "≥ N" (see ``add``);
* an invocation without a finish time (still running, or cut off by ``until``) is listed as
  pending and excluded from totals, so a report never includes its own pending review;
* validation time sums executed (not reused) runner validation checks; delivery checks are
  reported separately. Worker-authored check records are project data and are not read.
"""
from __future__ import annotations

from html import escape
import json
from pathlib import Path

from linear_runner.reporting import records
from linear_runner.reporting.records import USAGE_KEYS, parse_time

SCHEMA = "linear-runner.trajectory/1"
SEMANTICS = [
    "Invocation deduplication key is exact session ID + start + role; copied paths retained.",
    "Latest observed cumulative counter per unique session contributes once; per-invocation delta subtracts "
    "the session's prior counter.",
    "After an invocation of the same session without a counter (a failed turn), the next delta also holds that "
    "turn's unreported usage: it is an upper bound (basis cumulative-upper-bound, shown as ≤ N).",
    "Cached input is a subset of input and reasoning is a subset of output.",
    "Missing counters are unknown (null), never zero. No billed cost is inferred.",
    "A total is exact only when every attempt in it reported the figure; otherwise it is shown as ≥ N, the sum of "
    "what was reported (an attempt without a counter may have used more). A total is never an upper bound because "
    "of a ≤ attempt: that attempt's figure can only hold usage the counterless attempt before it did not report.",
    "Invocations without a recorded finish (or starting after the cutoff) are pending and excluded from totals.",
    "Validation seconds sum executed runner validation checks; delivery checks are reported separately and both "
    "count in the batch check seconds. Reused records add nothing. Worker-authored check records are not read.",
    "Issue elapsed is first invocation start to last invocation end; active wall sums invocation wall time.",
]


def _sum(values):
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def _delta(counter, previous):
    if counter is None:
        return None
    return {k: (counter[k] - (previous or {}).get(k, 0)) if k in counter else None for k in USAGE_KEYS}


# --- Figures: a value and how exact it is --------------------------------------------------
# Shared by the report tables and the runner's run summary comments, so both show the same
# numbers with the same marks.

EXACT, UPPER, LOWER = "exact", "upper-bound", "lower-bound"
MARKS = {EXACT: "", UPPER: "≤ ", LOWER: "≥ ", None: ""}
FIGURE_KEYS = (*USAGE_KEYS, "tool_calls", "failed_tool_calls", "model_seconds")


def figure(value, bound=EXACT):
    """``value`` with how exact it is; an unknown value (``None``) has no bound."""
    return {"value": value, "bound": bound if value is not None else None}


def add(figures):
    """The total of ``figures``: the sum of the known values, marked

    * exact when every part is known and exact;
    * a lower bound ("≥ N") when any part is unknown or a lower bound: what was not reported
      is missing from N. An upper-bound part does not change this: its excess can only be
      usage that an earlier counterless attempt of the same session did not report, and that
      attempt is in the same total (so the total never over-counts);
    * an upper bound ("≤ N") when some part is an upper bound and nothing is unknown;
    * unknown when nothing is known.
    """
    figures = list(figures)
    known = [f for f in figures if f["value"] is not None]
    if not known:
        return figure(None)
    if len(known) < len(figures) or any(f["bound"] == LOWER for f in known):
        bound = LOWER
    elif any(f["bound"] == UPPER for f in known):
        bound = UPPER
    else:
        bound = EXACT
    return figure(sum(f["value"] for f in known), bound)


def attempt_figures(attempt):
    """One attempt's figures: its usage delta (an upper bound for basis
    ``cumulative-upper-bound``), completed and failed tool calls, and wall seconds."""
    delta = attempt.get("usage_delta") or {}
    bound = UPPER if attempt.get("usage_basis") == records.UPPER_BOUND else EXACT
    values = {key: figure(delta.get(key), bound) for key in USAGE_KEYS}
    values.update(tool_calls=figure(attempt.get("tool_calls")), failed_tool_calls=figure(attempt.get("failed_tool_calls")),
                  model_seconds=figure(attempt.get("wall_seconds")))
    return values


def check_figure(checks):
    """Seconds of the executed (not reused) runner checks; unknown when none ran."""
    executed = [c for c in checks if not c.get("reused")]
    return add(figure(c.get("seconds")) for c in executed)


def totals(attempts, checks):
    """Totals over finished ``attempts`` (``build`` rows) and runner ``checks`` (audit or
    ``find_checks`` records): every FIGURE_KEYS figure, ``check_seconds`` and ``seconds``
    (model plus check time; waiting between them is not counted)."""
    rows = [attempt_figures(a) for a in attempts]
    result = {key: add(r[key] for r in rows) for key in FIGURE_KEYS}
    result["check_seconds"] = check_figure(checks)
    time = [result["model_seconds"]] if attempts else []
    if any(not c.get("reused") for c in checks):
        time.append(result["check_seconds"])
    result["seconds"] = add(time)
    return result


# --- Run summaries: what the runner posts after Done (or when an issue is set aside) --------

PHASE_NAMES = {"implement": "Implement", "repair": "Repair", "review": "Review"}
LIGHT_REVIEW_SOURCE = "low-risk review rule"


def stage_name(attempt):
    if attempt["phase"] == "review" and attempt.get("selection_source") == LIGHT_REVIEW_SOURCE:
        return "Lighter review"
    return PHASE_NAMES.get(attempt["phase"], str(attempt["phase"]).capitalize())


def attempt_outcome(attempt):
    """``blocked`` or ``failed`` (the session ended without a result), else None."""
    if attempt.get("status") == "blocked":
        return "blocked"
    if attempt.get("status") is None and attempt.get("exit_code") not in (None, 0):
        return "failed"
    return None


def stage_labels(attempts, outcomes=True):
    """Labels for one issue's attempts in start order: repairs are always numbered, another
    stage when it ran more than once; a repair of an independent review's findings
    (``recover repair``) is "Repair N (from review)"; with ``outcomes`` a blocked or failed
    attempt says so ("Repair 2 (blocked)", "Repair 1 (from review, blocked)")."""
    names = [stage_name(a) for a in attempts]
    seen, labels = {}, []
    for attempt, name in zip(attempts, names):
        seen[name] = seen.get(name, 0) + 1
        label = f"{name} {seen[name]}" if name == "Repair" or names.count(name) > 1 else name
        notes = ["from review"] if attempt["phase"] == "repair" and attempt.get("repair_source") == "review" else []
        if outcomes and attempt_outcome(attempt):
            notes.append(attempt_outcome(attempt))
        labels.append(label + (f" ({', '.join(notes)})" if notes else ""))
    return labels


def run_summary(result, issue):
    """One issue's run summary from ``build`` output: a row per finished model attempt in start
    order (``stage_labels``, model, effort and ``attempt_figures``), the runner's check time and
    the issue's ``totals`` (the same figures as the report's per-issue row)."""
    attempts = [a for a in result["attempts"] if a["issue"] == issue]
    checks = [c for c in result["validation_audit"] if c["issue"] == issue]
    rows = []
    for attempt, label in zip(attempts, stage_labels(attempts)):
        rows.append(dict(attempt_figures(attempt), stage=label,
                         model=attempt["requested_model"], effort=attempt["requested_effort"],
                         attempt=attempt["attempt"]))
    summary = next((s for s in result["summaries"] if s["issue"] == issue), None)
    return {"issue": issue, "attempts": len(attempts), "rows": rows,
            "checks": sum(not c.get("reused") for c in checks), "check_seconds": check_figure(checks),
            "total": summary["totals"] if summary else totals([], []),
            "pending": sum(p["issue"] == issue for p in result["pending"])}


def batch_summary(result, outcomes):
    """Per-issue totals for ``outcomes`` ({issue: outcome word}, in table order) and the batch
    total over all their attempts and checks (never a sum of rounded rows)."""
    issues = list(outcomes)
    summaries = {s["issue"]: s for s in result["summaries"]}
    rows = [dict(summaries[i]["totals"] if i in summaries else totals([], []), issue=i, outcome=outcomes[i],
                 attempts=summaries[i]["attempts"] if i in summaries else 0) for i in issues]
    total = totals([a for a in result["attempts"] if a["issue"] in issues],
                   [c for c in result["validation_audit"] if c["issue"] in issues])
    return {"rows": rows, "total": dict(total, attempts=sum(r["attempts"] for r in rows))}


def issue_order(issue):
    prefix, _, number = issue.rpartition("-")
    return (prefix, int(number)) if number.isdigit() else (issue, 0)


def build(invocations, checks, *, issues=None, until=None, groups=None, captured_at=None):
    """Summaries from deduplicated invocations and check records (see module docstring)."""
    cutoff = parse_time(until) if until else None
    wanted = list(issues) if issues else sorted({i["issue"] for i in invocations} | {c["issue"] for c in checks},
                                                key=issue_order)
    selected, pending = [], []
    for item in invocations:
        if item["issue"] not in wanted:
            continue
        start, end = parse_time(item["start"]), parse_time(item["end"])
        if cutoff and start > cutoff:
            continue
        if end is None or (cutoff and end > cutoff):
            pending.append(dict(item, pending_reason="no recorded finish" if end is None else "finished after cutoff"))
            continue
        selected.append(item)
    kept_checks = [c for c in checks if c["issue"] in wanted
                   and not (cutoff and parse_time(c["started_at"]) and parse_time(c["started_at"]) > cutoff)]

    # Sessions: the latest observed counter counts once; deltas per invocation.
    sessions = {}
    attempts = []
    for item in selected:
        key = item["session_id"] or f"unknown:{item['source']}"
        session = sessions.setdefault(key, {"session_id": item["session_id"], "issue": item["issue"],
                                            "role": item["role"], "invocations": 0, "counter": None,
                                            "monotonic": True, "gap": False})
        previous = session["counter"]
        basis = records.delta_basis(item, session["gap"])
        if item["counter"] is None:
            session["gap"] = True
        else:
            if previous and any(item["counter"].get(k, 0) < previous.get(k, 0) for k in USAGE_KEYS):
                session["monotonic"] = False
            session["counter"] = {k: item["counter"][k] for k in USAGE_KEYS if k in item["counter"]}
            session["gap"] = False
        session["invocations"] += 1
        attempts.append({
            "issue": item["issue"], "run_id": item["run_id"], "attempt": item["attempt"], "phase": item["phase"],
            "role": item["role"], "session_id": item["session_id"], "start": item["start"], "end": item["end"],
            "wall_seconds": item["wall_seconds"], "exit_code": item["exit_code"], "status": item["status"],
            "profile": item["profile"], "backend": item.get("backend"),
            "selection_source": item.get("selection_source"), "requested_model": item["requested_model"],
            "requested_effort": item["requested_effort"], "observed_models": item["observed_models"],
            "prompt_bytes": item["prompt_bytes"], "tool_output_bytes": item["tool_output_bytes"],
            "tool_calls": item.get("tool_calls"), "failed_tool_calls": item.get("failed_tool_calls"),
            "handoff": item.get("handoff"), "repair_source": item.get("repair_source"),
            "compact_token_limit": item.get("compact_token_limit"),
            "cumulative_usage": {k: item["counter"][k] for k in USAGE_KEYS if k in item["counter"]}
            if item["counter"] is not None else None,
            "usage_delta": _delta(item["counter"], previous if item["counter"] is not None else None),
            "usage_basis": basis,
            "source": item["source"], "copies": len(item["copies"])})
    for session in sessions.values():
        session["covered"] = session["counter"] is not None
        del session["gap"]

    summaries = []
    for issue in wanted:
        mine = [a for a in attempts if a["issue"] == issue]
        own_sessions = [s for s in sessions.values() if s["issue"] == issue]
        own_checks = [c for c in kept_checks if c["issue"] == issue]
        usage = {}
        for key in USAGE_KEYS:
            values = [(s["counter"] or {}).get(key) for s in own_sessions]
            usage[key] = sum(values) if own_sessions and all(v is not None for v in values) else None
        starts = [parse_time(a["start"]) for a in mine]
        ends = [parse_time(a["end"]) for a in mine]
        summaries.append({
            "issue": issue, "attempts": len(mine), "sessions": len(own_sessions),
            "covered_sessions": sum(s["covered"] for s in own_sessions),
            "active_agent_wall_seconds": _sum(a["wall_seconds"] for a in mine),
            "issue_elapsed_seconds": (max(ends) - min(starts)).total_seconds() if mine else None,
            "validation_seconds": _sum(c["seconds"] for c in own_checks if c["stage"] != "delivery"),
            "delivery_check_seconds": _sum(c["seconds"] for c in own_checks if c["stage"] == "delivery"),
            "max_recorded_check_rss_kib": max((c["rss_kib"] for c in own_checks if c["rss_kib"] is not None),
                                              default=None),
            "repairs": sum(a["phase"] == "repair" for a in mine),
            "reviews": sum(a["phase"] == "review" for a in mine),
            "pending": sum(p["issue"] == issue for p in pending),
            "usage": usage, "totals": totals(mine, own_checks)})

    audit = []
    for check in kept_checks:
        audit.append({k: check[k] for k in ("issue", "run_id", "stage", "name", "exit_code", "status", "reused", "started_at",
                                             "seconds", "rss_kib", "log", "log_found", "hash_matches")})

    comparison = []
    for label, members in (groups or {"all": wanted}).items():
        rows = [s for s in summaries if s["issue"] in members]
        group_sessions = [s for s in sessions.values() if s["issue"] in members]
        group_totals = totals([a for a in attempts if a["issue"] in members],
                              [c for c in kept_checks if c["issue"] in members])
        sums = {key: (sum(r["usage"][key] for r in rows) if rows and all(r["usage"][key] is not None for r in rows)
                      else None) for key in USAGE_KEYS}
        comparison.append({
            "batch": label, "issues": len(rows), "recorded_invocations": sum(r["attempts"] for r in rows),
            "sessions": len(group_sessions), "covered": sum(s["covered"] for s in group_sessions),
            "active_seconds": _sum(r["active_agent_wall_seconds"] for r in rows),
            "recorded_check_seconds": _sum([r["validation_seconds"] for r in rows]
                                           + [r["delivery_check_seconds"] for r in rows]),
            "input_tokens": sums["input_tokens"], "cached_subset": sums["cached_input_tokens"],
            "output_tokens": sums["output_tokens"], "reasoning_subset": sums["reasoning_output_tokens"],
            "totals": group_totals})

    return {"schema": SCHEMA, "captured_at": captured_at, "until": until, "issues": wanted,
            "semantics": SEMANTICS, "summaries": summaries, "attempts": attempts,
            "sessions": sorted(sessions.values(), key=lambda s: (issue_order(s["issue"]), s["session_id"] or "")),
            "pending": [{k: p[k] for k in ("issue", "attempt", "phase", "session_id", "start", "end", "pending_reason")}
                        for p in pending],
            "validation_audit": audit, "comparison": comparison}


def from_roots(roots, **options):
    return build(records.find_invocations(roots), records.find_checks(roots), **options)


# --- Reproduction check -------------------------------------------------------------------

SUMMARY_FIELDS = ("attempts", "sessions", "covered_sessions", "active_agent_wall_seconds", "issue_elapsed_seconds",
                  "validation_seconds", "max_recorded_check_rss_kib")
COMPARISON_FIELDS = ("issues", "recorded_invocations", "sessions", "covered", "active_seconds",
                     "recorded_check_seconds", "input_tokens", "cached_subset", "output_tokens", "reasoning_subset")


def _same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))
    return a == b


def compare_recorded(result, trajectory=None, comparison=None, comparison_label=None):
    """Field-by-field match against a recorded trajectory (``summaries``) and comparison rows."""
    rows = []
    ours = {s["issue"]: s for s in result["summaries"]}
    for recorded in (trajectory or {}).get("summaries", []):
        mine = ours.get(recorded.get("issue"))
        if mine is None:
            continue
        fields = [(f, recorded.get(f), mine.get(f)) for f in SUMMARY_FIELDS if f in recorded]
        fields += [(f"usage.{k}", recorded["usage"][k], mine["usage"].get(k)) for k in USAGE_KEYS
                   if k in (recorded.get("usage") or {})]
        for name, expected, actual in fields:
            rows.append({"scope": recorded["issue"], "field": name, "recorded": expected, "rendered": actual,
                         "match": _same(expected, actual)})
    if comparison is not None:
        recorded_rows = comparison if isinstance(comparison, list) else [comparison]
        for recorded in recorded_rows:
            if comparison_label and recorded.get("batch") != comparison_label:
                continue
            for mine in result["comparison"]:
                for name in COMPARISON_FIELDS:
                    if name in recorded:
                        rows.append({"scope": f"{recorded.get('batch')} vs {mine['batch']}", "field": name,
                                     "recorded": recorded[name], "rendered": mine[name],
                                     "match": _same(recorded[name], mine[name])})
    return rows


# --- Rendering -------------------------------------------------------------------------------

def _fmt(value):
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:,.1f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def figure_text(value, number=None):
    """A figure as text: its mark ("≤ " or "≥ ") and the formatted value; unknown stays unknown."""
    if value["value"] is None:
        return "unknown"
    return MARKS[value["bound"]] + (number or _fmt)(value["value"])


def delta_cell(attempt, key):
    """An attempt's delta for ``key``; an upper bound is shown as ``≤ N``, never as an exact delta."""
    value = (attempt["usage_delta"] or {}).get(key)
    if value is not None and attempt.get("usage_basis") == records.UPPER_BOUND:
        return f"≤ {_fmt(value)}"
    return value


def figure_cell(value):
    """A table cell: the plain number when exact (right-aligned in HTML), else its marked text."""
    return value["value"] if value["value"] is None or value["bound"] == EXACT else figure_text(value)


def tables(result):
    """(title, intro, header, rows) for every section; shared by Markdown and HTML."""
    usage_header = ["Issue", "Attempts", "Sessions", "Covered", "Active wall s", "Elapsed s", "Validation s",
                    "Delivery checks s", "Input", "Cached (subset)", "Output", "Reasoning (subset)", "Tool calls",
                    "Failed tool calls"]
    usage_rows = [[s["issue"], s["attempts"], s["sessions"], s["covered_sessions"], s["active_agent_wall_seconds"],
                   s["issue_elapsed_seconds"], s["validation_seconds"], s["delivery_check_seconds"],
                   *(figure_cell(s["totals"][k]) for k in (*USAGE_KEYS, "tool_calls", "failed_tool_calls"))]
                  for s in result["summaries"]]
    attempt_header = ["Issue", "Attempt", "Stage", "Profile", "Model/effort", "Status", "Wall s", "Prompt bytes",
                      "Tool output bytes", "Tool calls", "Cumulative input", "Delta input", "Copies"]
    stages = {}
    for issue in dict.fromkeys(a["issue"] for a in result["attempts"]):
        mine = [a for a in result["attempts"] if a["issue"] == issue]
        stages.update(zip((id(a) for a in mine), stage_labels(mine, outcomes=False)))
    attempt_rows = [[a["issue"], a["attempt"], stages[id(a)], a["profile"],
                     f"{a['requested_model']}/{a['requested_effort']}", a["status"] or f"exit {a['exit_code']}",
                     a["wall_seconds"], a["prompt_bytes"], a["tool_output_bytes"], a.get("tool_calls"),
                     (a["cumulative_usage"] or {}).get("input_tokens"), delta_cell(a, "input_tokens"),
                     a["copies"]] for a in result["attempts"]]
    session_header = ["Issue", "Session", "Role", "Invocations", "Latest input", "Covered", "Monotonic"]
    session_rows = [[s["issue"], s["session_id"], s["role"], s["invocations"], (s["counter"] or {}).get("input_tokens"),
                     s["covered"], s["monotonic"]] for s in result["sessions"]]
    audit_header = ["Issue", "Stage", "Check", "Exit", "Outcome", "Reused", "Seconds", "Max RSS KiB",
                    "Log hash matches"]
    audit_rows = [[c["issue"], c["stage"], c["name"], c["exit_code"], records.outcome_text(c.get("status")), c["reused"],
                   c["seconds"], c["rss_kib"], c["hash_matches"]] for c in result["validation_audit"]]
    comparison_header = ["Batch", "Issues", "Invocations", "Sessions", "Covered", "Active s", "Check s", "Input",
                         "Cached (subset)", "Output", "Reasoning (subset)", "Tool calls"]
    comparison_rows = [[c["batch"], c["issues"], c["recorded_invocations"], c["sessions"], c["covered"],
                        c["active_seconds"], c["recorded_check_seconds"],
                        *(figure_cell(c["totals"][k]) for k in (*USAGE_KEYS, "tool_calls"))]
                       for c in result["comparison"]]
    sections = [
        ("Usage and time per issue", "Totals per issue from the saved session and check records. ≥ N marks a "
         "lower bound: an attempt reported no figure, so it may have used more than N.", usage_header, usage_rows),
        ("Attempts", "One row per recorded model invocation, in start order; stages are named as in the run "
         "summary (\"Repair N (from review)\" repaired an independent review's findings). ≤ marks an upper bound: an "
         "earlier invocation of the same session reported no usage.", attempt_header, attempt_rows),
        ("Sessions", "Latest cumulative counter per unique session; it counts once.", session_header, session_rows),
        ("Validation audit", "Runner check records with their log hashes re-verified.", audit_header, audit_rows),
        ("Batch comparison", "Group totals; unlike workloads are not causal comparisons.", comparison_header,
         comparison_rows),
    ]
    if result["pending"]:
        sections.append(("Pending", "Invocations not counted because they had not finished when the report was made.",
                         ["Issue", "Attempt", "Phase", "Start", "Reason"],
                         [[p["issue"], p["attempt"], p["phase"], p["start"], p["pending_reason"]]
                          for p in result["pending"]]))
    return sections


def render_markdown(result, reproduction=None):
    lines = ["# Trajectory and usage report", "",
             f"Rendered from saved runner records; no model was used. Cutoff: {result['until'] or 'none'}.", ""]
    for title, intro, header, rows in tables(result):
        lines += [f"## {title}", "", intro, "", "| " + " | ".join(header) + " |",
                  "|" + "|".join("---" for _ in header) + "|"]
        lines += ["| " + " | ".join(_fmt(v).replace("|", "\\|") for v in row) + " |" for row in rows]
        lines.append("")
    if reproduction is not None:
        matched = sum(r["match"] for r in reproduction)
        lines += ["## Reproduction check", "", f"{matched} of {len(reproduction)} recorded values match.", "",
                  "| Scope | Field | Recorded | Rendered | Match |", "|---|---|---|---|---|"]
        lines += [f"| {r['scope']} | {r['field']} | {_fmt(r['recorded'])} | {_fmt(r['rendered'])} | "
                  f"{'yes' if r['match'] else 'NO'} |" for r in reproduction]
        lines.append("")
    lines += ["## Semantics", ""] + [f"* {s}" for s in result["semantics"]] + [""]
    return "\n".join(lines)


def render_html(result, reproduction=None):
    style = ("body{font:15px system-ui,sans-serif;max-width:1200px;margin:32px auto;padding:0 16px;color:#1b1f24;"
             "background:#fff}table{border-collapse:collapse;margin:8px 0 24px;font-size:13px}td,th{padding:4px 8px;"
             "border-bottom:1px solid #d0d7de;text-align:left}td.n{text-align:right;font-variant-numeric:tabular-nums}"
             ".no{color:#b42318;font-weight:600}@media (prefers-color-scheme:dark){body{background:#0d1117;"
             "color:#e6edf3}td,th{border-color:#30363d}}")
    parts = ['<!doctype html><html lang="en"><meta charset="utf-8"><title>Trajectory report</title>',
             f"<style>{style}</style><h1>Trajectory and usage report</h1>",
             f"<p>Rendered from saved runner records; no model was used. Cutoff: {escape(str(result['until'] or 'none'))}.</p>"]
    for title, intro, header, rows in tables(result):
        parts.append(f"<h2>{escape(title)}</h2><p>{escape(intro)}</p><table><tr>"
                     + "".join(f"<th>{escape(h)}</th>" for h in header) + "</tr>")
        for row in rows:
            parts.append("<tr>" + "".join(
                f'<td class="n">{escape(_fmt(v))}</td>' if isinstance(v, (int, float)) and not isinstance(v, bool)
                else f"<td>{escape(_fmt(v))}</td>" for v in row) + "</tr>")
        parts.append("</table>")
    if reproduction is not None:
        matched = sum(r["match"] for r in reproduction)
        parts.append(f"<h2>Reproduction check</h2><p>{matched} of {len(reproduction)} recorded values match.</p>"
                     "<table><tr><th>Scope</th><th>Field</th><th>Recorded</th><th>Rendered</th><th>Match</th></tr>")
        for r in reproduction:
            parts.append(f"<tr><td>{escape(r['scope'])}</td><td>{escape(r['field'])}</td>"
                         f"<td class=\"n\">{escape(_fmt(r['recorded']))}</td><td class=\"n\">{escape(_fmt(r['rendered']))}</td>"
                         + ("<td>yes</td>" if r["match"] else '<td class="no">NO</td>') + "</tr>")
        parts.append("</table>")
    parts.append("<h2>Semantics</h2><ul>" + "".join(f"<li>{escape(s)}</li>" for s in result["semantics"]) + "</ul></html>")
    return "\n".join(parts)


def write(directory, result, *, stem="trajectory", formats=("md", "html"), reproduction=None):
    """Write ``<stem>.json`` plus the requested human formats; return {format: path}."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    payload = dict(result, reproduction=reproduction) if reproduction is not None else result
    paths = {"json": directory / f"{stem}.json"}
    paths["json"].write_text(json.dumps(payload, indent=2) + "\n")
    if "md" in formats:
        paths["md"] = directory / f"{stem}.md"
        paths["md"].write_text(render_markdown(result, reproduction))
    if "html" in formats:
        paths["html"] = directory / f"{stem}.html"
        paths["html"].write_text(render_html(result, reproduction))
    return {k: str(v) for k, v in paths.items()}
