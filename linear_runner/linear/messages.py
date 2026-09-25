"""Build human-review Linear comments from saved state (pure functions, no I/O).

The runner and ``render_samples.py`` call the same builders, so the samples show exactly
what Linear would receive. Wording lives in ``templates/``; this module only chooses the
variant and fills fields with plain prose.
"""
from __future__ import annotations

import re
import shlex

from linear_runner.linear.updates import (first_sentence, issue_mentions, neutralize_issue_mentions, plain, quote,
                                          render, variant)

AUTH = '--reason "<why>" --authorized-by "<your name>"'


def context(config):
    """Values every message needs, from a resolved configuration."""
    from linear_runner.config import DEFAULT_HOME, batch_argument, find_home
    home = config.get("variables", {}).get("home")
    attention = config.get("attention", {})
    return {"batch": config["batch_id"], "batch_arg": batch_argument(config),
            "home": home if home and str(find_home(DEFAULT_HOME)) != home else None,
            "prefix": attention.get("command_prefix") or "python3 runner.py",
            "mention": attention.get("owner_mention") or "", "branch": config.get("branch", ""),
            "max_repairs": config.get("policy", {}).get("phases", {}).get("max_repairs", 2)}


def command(ctx, *args, auth=False):
    """One runner command line, e.g. ``python3 .../runner.py launch --batch my-batch``."""
    parts = [*args, "--batch", ctx["batch_arg"]]
    if ctx.get("home"):
        parts += ["--home", ctx["home"]]
    text = " ".join(str(p) if str(p).startswith("<") else shlex.quote(str(p)) for p in parts)
    return f"{ctx['prefix']} {text}" + (" " + AUTH if auth else "")


def block(sentence, line):
    """A plain sentence saying what a command does, then the command in its own bash block."""
    return f"{sentence}\n\n```bash\n{line}\n```"


def blocks(*items):
    return "\n\n".join(block(s, c) for s, c in items)


def plural(count, noun, suffix="s"):
    return f"{count} {noun}{'' if count == 1 else suffix}"


def listing(items):
    items = list(items)
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def deliverable_lines(items):
    """One line per deliverable: the path, then a short description when known."""
    lines = []
    for item in items or []:
        description = plain(item.get("description") or "", 160).rstrip(".")
        lines.append(f"- {item['path']}" + (f" — {description}" if description else ""))
    return "\n".join(lines)


def evidence(*paths):
    return ", ".join(str(p) for p in paths if p)


def criteria_phrase(count):
    return "the acceptance criterion" if count == 1 else f"all {count} acceptance criteria"


def unsatisfied(result):
    return [e for e in (result or {}).get("acceptance", []) if isinstance(e, dict) and e.get("satisfied") is not True]


def short_cause(text, limit=140):
    """One clause from an error message, safe inside a single-sentence headline."""
    value = " ".join(str(text or "unknown error").split()).rstrip(".")
    value = value.replace(". ", "; ").replace("! ", "; ").replace("? ", "; ")
    return value if len(value) <= limit else value[:limit].rsplit(" ", 1)[0] + " …"


def said(text, limit=140):
    """``short_cause`` of error text (which can quote a worker or an issue) as posted to Linear:
    issue mentions neutralized so the comment does not link issues."""
    return neutralize_issue_mentions(short_cause(text, limit))


def mention_warning(text, what="reason"):
    """A warning for ``recover`` when a recovery ``text`` that will be quoted in a Linear
    comment names an issue; None otherwise."""
    found = issue_mentions(text)
    if not found:
        return None
    return (f"Warning: the {what} names {', '.join(found)}. A bare issue mention in a Linear comment links the "
            "issues (Linear adds a related link); the recovery comment shows it as inline code ("
            + ", ".join(f"`{m}`" for m in found) + "), which Linear is assumed not to link. Name the issue "
            "another way if no link must appear.")


def model_text(stage, *, source=False):
    """``gpt-6-luna (max effort, Codex)``; with ``source``, how a named entry was chosen."""
    if not stage or not stage.get("model"):
        return ""
    from linear_runner.backends import BACKENDS
    backend = BACKENDS.get(stage.get("backend") or "codex")
    text = f"{stage['model']} ({stage.get('effort')} effort, {backend.label if backend else stage.get('backend')})"
    origin = stage.get("model_source") or ""
    if source and origin.startswith("issue label "):
        text += f", named by the issue label {origin[len('issue label '):].split(',')[0]}"
    elif source and origin.startswith("batch model_overrides"):
        text += ", named by the batch"
    return text


def stage_sentence(verb, stage, suffix=""):
    """``Implemented with gpt-6-luna (max effort, Codex).`` or "" without a recorded stage."""
    text = model_text(stage)
    return f"{verb} with {text}{suffix}." if text else ""


def planned_models(plan):
    """The claim's model plan: implementation, repairs if needed, and the review."""
    plan = plan or {}
    parts = []
    for phase, words in (("implement", "Implementation runs"), ("repair", "Repairs, if needed, run"),
                         ("review", "The review runs")):
        text = model_text(plan.get(phase), source=True)
        if phase == "repair" and text and text == model_text(plan.get("implement"), source=True):
            parts.append("Repairs, if needed, use the same model.")
        elif text:
            parts.append(f"{words} with {text}.")
    return " ".join(parts)


def stages_summary(stages):
    """The Done line: implemented, repaired (each distinct model in order) and reviewed with."""
    stages = list(stages or [])
    def last(phase):
        return next((s for s in reversed(stages) if s.get("phase") == phase), None)
    parts = []
    if last("implement"):
        parts.append(f"implemented with {model_text(last('implement'))}")
    repairs = [s for s in stages if s.get("phase") == "repair"]
    distinct = list(dict.fromkeys(model_text(s) for s in repairs))
    if distinct:
        parts.append("repaired with " + " then ".join(distinct))
    if last("review"):
        parts.append(f"reviewed with {model_text(last('review'))}")
    return ("It was " + listing(parts) + ".") if parts else ""


def draft_post(text, who, phase, stage=None):
    """A linted model draft as posted: unchanged, with a one-line attribution."""
    model = model_text(stage)
    return text.strip() + (f"\n\n_Written by the {who} in the {phase} phase with {model}; posted by the runner._"
                           if model else f"\n\n_Written by the {who} ({phase} phase); posted by the runner._")


# --- Runner-authored events -------------------------------------------------------------

def claim(ctx, *, issue, plan, check_count, criteria_count, run_dir):
    """``plan`` is {phase: selection} for implement, repair and review (as selected now)."""
    return render("claim", {"issue": issue, "profile": plan["implement"]["profile"], "models": planned_models(plan),
                            "check_count": plural(check_count, "configured check"),
                            "criteria_count": criteria_phrase(criteria_count), "evidence": evidence(run_dir)})


def ready(ctx, *, issue, result, attempt, draft_problem=None, deliverables=(), missing=(), stage=None):
    entries = result.get("acceptance", [])
    met = sum(e.get("satisfied") is True for e in entries if isinstance(e, dict))
    noun = "criterion" if len(entries) == 1 else "criteria"
    criteria = f"The worker marked {met} of {len(entries)} acceptance {noun} as met; the runner now runs the checks."
    if draft_problem:
        criteria += f" The worker's own ready note was not posted because {draft_problem}."
    limits = [plain(item, 200) for item in result.get("limitations", []) if str(item).strip()]
    if missing:
        criteria += f" Listed deliverables that were not found: {listing(missing)}."
    verb = "Repaired" if (stage or {}).get("phase") == "repair" else "Implemented"
    return render("ready", {"issue": issue, "model": stage_sentence(verb, stage),
                            "summary": quote(result.get("summary")), "criteria": criteria,
                            "deliverables": deliverable_lines(deliverables),
                            "limitations": " ".join(l if l.endswith(".") else l + "." for l in limits),
                            "evidence": evidence(attempt)})


def validation(ctx, *, issue, records, passed, repair=None, directory=None, repair_stage=None, escalated=False):
    from linear_runner.engine.delivery import check_passed
    failing = [r for r in records if not check_passed(r)]
    empty = [r for r in records if r.get("status") == "empty" and check_passed(r)]
    reused = sum(bool(r.get("reused")) for r in records)
    checks = plural(len(records), "check") + (f", {reused} reused from unchanged inputs" if reused else "")
    fields = {"issue": issue, "checks": checks, "failed": len(failing), "total": len(records), "repair": repair,
              "max_repairs": ctx["max_repairs"], "evidence": evidence(directory),
              "empty": " ".join(f"{r.get('name', 'check')} selected no tests (allowed)." for r in empty)}
    if not passed:
        fields["failing"] = " ".join(f"{r.get('name', 'check')} exited with code {r.get('exit_code')}." for r in failing)
        fields["repair_note"] = ("The repair worker gets the failing check logs and may change only what those "
                                 "checks need.")
        if model_text(repair_stage):
            fields["repair_note"] += (f" Repair {repair} runs with {model_text(repair_stage, source=True)}"
                                      + (f", escalated to the {repair_stage.get('profile')} profile" if escalated else "")
                                      + ".")
    return render("validation", fields, headline="passed" if passed else "repairing")


def review(ctx, *, issue, result, attempt, draft_problem=None, stage=None):
    count = len(result.get("acceptance", []))
    summary = quote(result.get("summary"))
    if draft_problem:
        summary += f"\n\nThe reviewer's note did not follow its template ({draft_problem}), so it is quoted here."
    return render("review", {"issue": issue, "criteria": criteria_phrase(count), "summary": summary,
                             "model": stage_sentence("Reviewed", stage),
                             "evidence": evidence(attempt)}, headline="accepted")


def done(ctx, *, issue, commit, criteria_count, repairs, run_dir, deliverables=(), stages=()):
    note = "" if not repairs else f"It needed {plural(repairs, 'repair')} before the checks passed."
    return render("done", {"issue": issue, "criteria": criteria_phrase(criteria_count), "commit": commit[:12],
                           "branch": ctx["branch"], "repairs": note, "deliverables": deliverable_lines(deliverables),
                           "models": stages_summary(stages),
                           "evidence": evidence(run_dir, f"{run_dir}/final-result.json")},
                  headline="deliverables" if deliverables else "default")


def own_words(*, who, result=None, draft=None, draft_problem=None, draft_path=None):
    """The worker's or reviewer's explanation: a valid blocked draft, else its result."""
    if draft:
        return f"The {who} wrote:\n\n" + quote(draft)
    parts = []
    summary = (result or {}).get("summary")
    if summary:
        parts.append(f"The {who} wrote:\n\n" + quote(summary))
    missing = unsatisfied(result)
    if missing:
        lines = [f"- {plain(e.get('criterion'), 160)}: {plain(e.get('evidence') or 'no evidence given', 240)}"
                 for e in missing[:6]]
        parts.append(f"Criteria the {who} marked as not met:\n" + "\n".join(lines))
    if draft_problem:
        parts.append(f"The {who}'s blocked note was not posted because {draft_problem}; it is kept at {draft_path}.")
    return "\n\n".join(parts)


def recovery_steps(ctx, *, issue=None, event=None, step=None, phase=None, classification=None, repairs=None):
    """The exact commands to continue after a stop, each in its own bash block.

    At the repair step the wording depends on how the repair ended: one that finished with
    status blocked offers ``revalidate`` (the owner fixed configuration or the environment),
    ``resume --note-file`` (the worker tries again, while repairs are left) and ``defer``;
    only a truly interrupted repair is called interrupted.
    """
    launch = ("Then start the batch again:", command(ctx, "launch"))
    if issue is None:
        return blocks(("Record the recovery:", command(ctx, "recover", "resume", auth=True)), launch)
    defer = command(ctx, "recover", "defer", "--issue", issue, "--restore-worktree", auth=True)
    aside = ("Or set the issue aside instead and let the batch continue:", defer)
    revalidate = command(ctx, "recover", "revalidate", auth=True)
    if step == "repair" and event == "worker_blocked":
        items = [("If you fixed the configuration or the environment the failing checks depend on, re-run the "
                  "checks on the current source without a model (no repair slot is used):", revalidate)]
        left = None if repairs is None else ctx["max_repairs"] - repairs
        if left is None or left > 0:
            items.append(("Or, to let the worker try one more repair, give it a note (the repair uses the next "
                          "repair slot):", command(ctx, "recover", "resume", "--note-file", "<note file>", auth=True)))
        return blocks(*items, launch, aside)
    if step == "repair":
        return blocks(("An interrupted repair cannot be resumed, but you can re-run the checks on the current source "
                       "without a model (the interrupted repair keeps its used slot):", revalidate), launch, aside)
    if event == "checks_failed":
        return blocks(("After fixing the configuration or the environment the failing checks depend on, re-run the "
                       "checks on the current source without a model:", revalidate), launch, aside)
    if event == "budget_exceeded":
        primary = ("Record the new budget allowance for the phase:",
                   command(ctx, "recover", "budget", "--phase", phase or "<phase>", "--input-tokens", "<N>",
                           "--output-tokens", "<N>", "--tool-calls", "<N>", auth=True))
    elif event == "review_blocked" or step == "review":
        primary = ("Record a review-only recovery (you can add --note-file with a note for the reviewer, or "
                   "--repin-contract after clarifying a criterion):", command(ctx, "recover", "review", auth=True))
    elif step in ("publish", "done"):
        primary = ("Record a publish-only recovery (no model runs; add --accept-contract-drift when the issue changed "
                   "only outside the accepted criteria and scope):", command(ctx, "recover", "publish", auth=True))
    else:
        primary = ("Record the recovery (you can add --note-file with a note for the worker):",
                   command(ctx, "recover", "resume", auth=True))
    if classification in ("environment", "runner-defect"):
        return blocks(primary, launch)
    return blocks(primary, launch, aside)


def claude_auth_variant(error):
    """``claude-auth-token`` or ``claude-auth-login`` for a stop naming a Claude authentication
    failure and its auth mode (``Claude authentication (<mode>) ...``), else None."""
    from linear_runner.backends.claude import AUTH_FAILURE, TOKEN_MODES
    match = re.search(re.escape(AUTH_FAILURE) + r" \(([a-z-]+)\)", str(error or ""))
    if not match:
        return None
    return "claude-auth-token" if match.group(1) in TOKEN_MODES else "claude-auth-login"


def blocked(ctx, *, issue, classification, event=None, error="", step=None, phase=None, result=None, who="worker",
            draft=None, draft_problem=None, draft_path=None, evidence_paths=(), repairs=None, stage=None):
    subject = issue or f"Batch {ctx['batch']}"
    values = {"subject": subject, "phase": phase or step or "model", "step": step or "current",
              "error": said(error, 300)}
    known = event in ("worker_blocked", "review_blocked", "checks_failed", "delivery_failed", "budget_exceeded")
    cause = variant("blocked", "cause", event, values) if known else said(error)
    happened = variant("blocked", "happened", event if known else "other", values)
    if event in ("review_blocked", "checks_failed", "delivery_failed"):
        happened += f" The runner reported: {said(error, 300)}."
    if model_text(stage):
        happened += f" The {stage.get('phase')} phase ran with {model_text(stage)}."
    auth = claude_auth_variant(error) if classification == "environment" and not known else None
    needed = variant("blocked", "needed", "repair_blocked" if event == "worker_blocked" and step == "repair"
                     else event if known else auth or classification, values)
    words = own_words(who=who, result=result, draft=draft, draft_problem=draft_problem, draft_path=draft_path) \
        if event in ("worker_blocked", "review_blocked", "checks_failed", "budget_exceeded") else ""
    return render("blocked", {"subject": subject, "mention": ctx["mention"], "cause": cause, "what_happened": happened,
                              "own_words": words, "needed": needed,
                              "continue_steps": recovery_steps(ctx, issue=issue, event=event, step=step, phase=phase,
                                                               classification=classification, repairs=repairs),
                              "evidence": evidence(*evidence_paths)}, headline=classification)


def deferred(ctx, *, issue, cause, block, result=None, who="worker", draft=None, evidence_paths=()):
    if cause.get("rule_text"):
        why = f"The issue's own decision rule \"{neutralize_issue_mentions(cause['rule_text'])}\" matched block {block['id']}."
    else:
        why = f"The batch policy {cause.get('policy', 'on_block')} applies to block {block['id']}."
    why += f" It blocked at the {block.get('step', 'current')} step: {said(block.get('error'), 240)}."
    restore = ("It is not accepted and keeps its Linear state.\n\n" + blocks(
        ("Once the batch has stopped, record the restore:", command(ctx, "recover", "resume", "--issue", issue, auth=True)),
        ("Then start the batch again:", command(ctx, "launch"))))
    return render("deferred", {"issue": issue, "why": why, "restore": restore,
                               "own_words": own_words(who=who, result=result, draft=draft),
                               "evidence": evidence(*evidence_paths)})


RECOVERY_KINDS = ("resume", "revalidate", "review", "repair", "budget", "publish", "defer")


def recovery(ctx, *, record, step=None, note=None, evidence_paths=(), stage=None):
    details = record.get("details", {})
    subject = details.get("issue") or f"batch {ctx['batch']}"
    values = {"subject": subject, "step": step or details.get("step") or "saved", "phase": details.get("phase", "")}
    kind = record["kind"] if record["kind"] in RECOVERY_KINDS else "resume"
    action = variant("recovery", "kind", kind, values)
    if kind == "review" and details.get("redeliver"):
        action += " Delivery is re-run first; the previous packet is kept."
    if kind == "publish" and details.get("accepted_contract_drift"):
        action += " " + variant("recovery", "drift", "publish", values)
    if kind == "repair":
        unmet = len((details.get("review") or {}).get("unsatisfied") or [])
        if unmet:
            action += (f" The reviewer marked {unmet} acceptance {'criterion' if unmet == 1 else 'criteria'} as not "
                       "met; the worker gets each one with the reviewer's evidence.")
    if kind == "resume" and details.get("repair_retry"):
        action += " The worker gets one more repair of the failing checks, with the owner's note."
    if model_text(stage):
        action += f" The {stage.get('phase')} phase runs with {model_text(stage, source=True)}."
    return render("recovery", {"kind": record["kind"], "subject": subject, "authorized_by": record["authorized_by"],
                               "action": action, "reason": plain(record["reason"], 300).rstrip(".") + ".",
                               "then": variant("recovery", "then", record.get("then") or "continue"),
                               "note": quote(note) if note else "", "evidence": evidence(*evidence_paths)})


def issues_prose(*, done=(), paused=None, deferred=(), waiting=None, pending=()):
    parts = []
    if done:
        parts.append(f"Done: {listing(done)}.")
    if paused:
        parts.append(f"Paused: {paused}.")
    if deferred:
        parts.append(f"Set aside: {listing(deferred)}.")
    if waiting:
        parts.append("Waiting on prerequisites: " + "; ".join(f"{i} (needs {listing(n)})" for i, n in waiting.items()) + ".")
    if pending:
        parts.append(f"Not started: {listing(pending)}.")
    return " ".join(parts) or "No issue was completed."


# --- Usage tables (run summary and batch totals) ----------------------------------------------
# Figures come from reporting.trajectory (``run_summary``/``batch_summary``), the same
# accounting as ``runner.py report``; this part only formats them.

MARKS = {"upper-bound": "≤ ", "lower-bound": "≥ "}
UNKNOWN = "—"


def amount(value):
    """A token count, compact: 812, 2.1k, 39k, 1.60M, 16.2M."""
    value = int(value)
    if abs(value) < 1_000:
        return str(value)
    if abs(value) < 9_950:
        return f"{value / 1e3:.1f}k"
    if abs(value) < 999_500:
        return f"{value / 1e3:.0f}k"
    if abs(value) < 9_995_000:
        return f"{value / 1e6:.2f}M"
    if abs(value) < 99_950_000:
        return f"{value / 1e6:.1f}M"
    return f"{value / 1e6:.0f}M"


def duration(seconds):
    """Seconds, compact: 45 s, 24 min, 1 h 05 min."""
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def figure_text(value, number=amount):
    """A figure (``{value, bound}``): "—" when unknown (never 0), else "≤ N"/"≥ N"/"N"."""
    if value is None or value.get("value") is None:
        return UNKNOWN
    return MARKS.get(value.get("bound"), "") + number(value["value"])


def input_cell(values):
    """Input with cached input (a subset) in parentheses; just the input when cached is unknown."""
    total, cached = values["input_tokens"], values["cached_input_tokens"]
    text = figure_text(total)
    if total["value"] is not None and cached["value"] is not None:
        mark = MARKS.get(cached["bound"], "") if cached["bound"] != total["bound"] else ""
        text += f" ({mark}{amount(cached['value'])})"
    return text


def tool_cell(values):
    text = figure_text(values["tool_calls"], str)
    failed = values["failed_tool_calls"]["value"]
    return text + (f" ({failed} failed)" if values["tool_calls"]["value"] is not None and failed else "")


def figure_cells(values):
    """Input (cached), Output, Tool calls and Time cells for one row of figures."""
    return [input_cell(values), figure_text(values["output_tokens"]), tool_cell(values),
            figure_text(values["seconds"] if "seconds" in values else values["model_seconds"], duration)]


def markdown_table(header, rows, total):
    bold = [f"**{cell}**" if cell else "" for cell in total]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return "\n".join(lines + ["| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |" for row in [*rows, bold]])


def usage_notes(cells):
    """What the marks in a usage table mean; only marks that appear in ``cells`` are explained."""
    text = " ".join(cells)
    notes = ["Input includes cached input, shown in parentheses; time is model and check time, not waiting; these "
             "are token counters, not billed cost."]
    if "≤" in text:
        notes.append("≤ marks an upper bound: that attempt's figure can include usage of an earlier failed attempt of "
                     "the same session that reported none.")
    if "≥" in text:
        notes.append("≥ marks a lower bound: an attempt reported no figure, so the total may be higher.")
    if UNKNOWN in cells:
        notes.append(f"{UNKNOWN} means no figure was recorded.")
    return " ".join(notes)


RUN_HEADER = ["Stage", "Model", "Effort", "Input (cached)", "Output", "Tool calls", "Time"]
BATCH_HEADER = ["Issue", "Outcome", "Attempts", "Input (cached)", "Output", "Tool calls", "Time"]


def run_summary_table(summary):
    """(table, notes) for ``trajectory.run_summary`` output."""
    rows = [[row["stage"], row["model"] or UNKNOWN, row["effort"] or UNKNOWN, *figure_cells(row)]
            for row in summary["rows"]]
    rows.append(["Checks", UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN, figure_text(summary["check_seconds"], duration)])
    total = ["Total", "", "", *figure_cells(summary["total"])]
    # The Checks row's dashes mean "not applicable"; they need no note.
    return markdown_table(RUN_HEADER, rows, total), usage_notes([c for row in rows[:-1] + [total] for c in row])


def batch_table(summary):
    """(table, notes) for ``trajectory.batch_summary`` output."""
    rows = [[row["issue"], row["outcome"], str(row["attempts"]), *figure_cells(row)] for row in summary["rows"]]
    total = ["Batch total", "", str(summary["total"]["attempts"]), *figure_cells(summary["total"])]
    return markdown_table(BATCH_HEADER, rows, total), usage_notes([c for row in rows + [total] for c in row])


def run_summary(ctx, *, issue, outcome, summary, evidence_paths=()):
    """The usage comment for one issue's run: after Done (``done``), or when the issue is set
    aside by a rule or ``on_block`` (``deferred``) or by its owner (``set-aside``)."""
    table, notes = run_summary_table(summary)
    return render("run-summary", {"issue": issue, "attempts": plural(summary["attempts"], "model attempt"),
                                  "table": table, "notes": notes, "evidence": evidence(*evidence_paths)},
                  headline=outcome)


def batch_finished(ctx, *, outcome, done, total, issues, usage=None, checkpoint=None, deferred=(), evidence_paths=()):
    """``usage`` is ``trajectory.batch_summary`` output (None when it could not be rendered)."""
    if outcome == "partial" and deferred:
        steps = blocks(("Restore a set-aside issue:", command(ctx, "recover", "resume", "--issue", "<issue>", auth=True)),
                       ("Then start the batch again:", command(ctx, "launch")))
    elif outcome in ("checkpoint", "stopped", "partial"):
        steps = block("After reviewing the state, relaunch the batch:", command(ctx, "launch", "--clear-stop"))
    else:
        steps = ""
    return render("batch-finished", {"batch": ctx["batch"], "total": total,
                                     "mention": ctx["mention"] if outcome != "complete" else "",
                                     "done_count": len(done), "checkpoint": checkpoint or "", "issues": issues,
                                     "usage": "\n\n".join(batch_table(usage)) if usage else
                                     "The usage table could not be rendered from the saved records; see the terminal "
                                     "trajectory report.", "continue_steps": steps,
                                     "evidence": evidence(*evidence_paths)}, headline=outcome)


def batch_paused(ctx, *, subject, where, issues, evidence_paths=()):
    return render("batch-paused", {"batch": ctx["batch"], "mention": ctx["mention"], "subject": subject, "where": where,
                                   "issues": issues,
                                   "continue_steps": f"Follow the steps in the comment on {where}.",
                                   "evidence": evidence(*evidence_paths)})


def watchdog(ctx, *, condition, subject, minutes=None, observed="", last_update="", evidence_paths=()):
    steps = blocks(("Check the saved state and the supervisor:", command(ctx, "status")),
                   ("If nothing is running, record a recovery:", command(ctx, "recover", "resume", auth=True)),
                   ("Then start the batch again:", command(ctx, "launch")))
    return render("watchdog", {"batch": ctx["batch"], "mention": ctx["mention"], "subject": subject, "minutes": minutes,
                               "observed": observed, "last_update": quote(last_update) if last_update else "",
                               "continue_steps": steps, "evidence": evidence(*evidence_paths)}, headline=condition)


def subject_line(text):
    """A notification subject: the comment's first sentence."""
    return first_sentence(text)[:200]
