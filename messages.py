"""Build human-review Linear comments from saved state (pure functions, no I/O).

The runner and ``render_samples.py`` call the same builders, so the samples show exactly
what Linear would receive. Wording lives in ``templates/``; this module only chooses the
variant and fills fields with plain prose.
"""
from __future__ import annotations

import shlex

from updates import first_sentence, plain, quote, render, variant

AUTH = '--reason "<why>" --authorized-by "<your name>"'


def context(config):
    """Values every message needs, from a resolved configuration."""
    from config import DEFAULT_HOME, find_home
    batch_file = next((v for k, v in config.get("_layers", {}).items() if k.startswith("batch ")), "<batch file>")
    home = config.get("variables", {}).get("home")
    attention = config.get("attention", {})
    return {"batch": config["batch_id"], "batch_file": batch_file,
            "home": home if home and str(find_home(DEFAULT_HOME)) != home else None,
            "python": (config.get("launcher") or {}).get("python") or "python3",
            "runner": str(__import__("pathlib").Path(__file__).resolve().parent / "runner.py"),
            "owner": attention.get("owner_mention") or "the owner", "branch": config.get("branch", ""),
            "max_repairs": config.get("policy", {}).get("phases", {}).get("max_repairs", 2)}


def command(ctx, *args, auth=False):
    parts = [ctx["python"], ctx["runner"], *args, "--batch", ctx["batch_file"]]
    if ctx.get("home"):
        parts += ["--home", ctx["home"]]
    text = " ".join(shlex.quote(str(p)) if not str(p).startswith("<") else str(p) for p in parts)
    return "`" + text + (" " + AUTH if auth else "") + "`"


def plural(count, noun, suffix="s"):
    return f"{count} {noun}{'' if count == 1 else suffix}"


def listing(items):
    items = list(items)
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


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


def draft_post(text, who, phase):
    """A linted model draft as posted: unchanged, with a one-line attribution."""
    return text.strip() + f"\n\n_Written by the {who} ({phase} phase); posted by the runner._"


# --- Runner-authored events -------------------------------------------------------------

def claim(ctx, *, issue, selection, check_count, criteria_count, run_dir):
    return render("claim", {"issue": issue, "profile": selection["profile"], "model": selection["model"],
                            "effort": selection["effort"], "check_count": plural(check_count, "configured check"),
                            "criteria_count": criteria_phrase(criteria_count), "evidence": evidence(run_dir)})


def ready(ctx, *, issue, result, attempt, draft_problem=None):
    entries = result.get("acceptance", [])
    met = sum(e.get("satisfied") is True for e in entries if isinstance(e, dict))
    noun = "criterion" if len(entries) == 1 else "criteria"
    criteria = f"The worker marked {met} of {len(entries)} acceptance {noun} as met; the runner now runs the checks."
    if draft_problem:
        criteria += f" The worker's own ready note was not posted because {draft_problem}."
    limits = [plain(item, 200) for item in result.get("limitations", []) if str(item).strip()]
    return render("ready", {"issue": issue, "summary": quote(result.get("summary")), "criteria": criteria,
                            "limitations": " ".join(l if l.endswith(".") else l + "." for l in limits),
                            "evidence": evidence(attempt)})


def validation(ctx, *, issue, records, passed, repair=None, directory=None):
    failing = [r for r in records if r.get("exit_code")]
    reused = sum(bool(r.get("reused")) for r in records)
    checks = plural(len(records), "check") + (f", {reused} reused from unchanged inputs" if reused else "")
    fields = {"issue": issue, "checks": checks, "failed": len(failing), "total": len(records), "repair": repair,
              "max_repairs": ctx["max_repairs"], "evidence": evidence(directory)}
    if not passed:
        fields["failing"] = " ".join(f"{r.get('name', 'check')} exited with code {r.get('exit_code')}." for r in failing)
        fields["repair_note"] = ("The repair worker gets the failing check logs and may change only what those "
                                 "checks need.")
    return render("validation", fields, headline="passed" if passed else "repairing")


def review(ctx, *, issue, result, attempt, draft_problem=None):
    count = len(result.get("acceptance", []))
    summary = quote(result.get("summary"))
    if draft_problem:
        summary += f"\n\nThe reviewer's note did not follow its template ({draft_problem}), so it is quoted here."
    return render("review", {"issue": issue, "criteria": criteria_phrase(count), "summary": summary,
                             "evidence": evidence(attempt)}, headline="accepted")


def done(ctx, *, issue, commit, criteria_count, repairs, run_dir):
    note = "" if not repairs else f"It needed {plural(repairs, 'repair')} before the checks passed."
    return render("done", {"issue": issue, "criteria": criteria_phrase(criteria_count), "commit": commit[:12],
                           "branch": ctx["branch"], "repairs": note,
                           "evidence": evidence(run_dir, f"{run_dir}/final-result.json")})


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


def recovery_steps(ctx, *, issue=None, event=None, step=None, phase=None, classification=None):
    """The exact commands to continue after a stop, one per line."""
    launch = f"- Then start it: {command(ctx, 'launch')}"
    if issue is None:
        return f"- Record the recovery: {command(ctx, 'recover', 'resume', auth=True)}\n{launch}"
    hint = ""
    if event == "budget_exceeded":
        primary = command(ctx, "recover", "budget", "--phase", phase or "<phase>", "--input-tokens", "<N>",
                          "--output-tokens", "<N>", "--tool-calls", "<N>", auth=True)
    elif event == "review_blocked" or step == "review":
        primary = command(ctx, "recover", "review", auth=True)
        hint = " (add `--note-file <file>` to give the reviewer a note, or `--repin-contract` after clarifying a criterion)"
    elif step in ("publish", "done"):
        primary = command(ctx, "recover", "publish", auth=True)
    elif step == "repair":
        primary = None
    else:
        primary = command(ctx, "recover", "resume", auth=True)
        hint = " (add `--note-file <file>` to give the worker a note)"
    defer = command(ctx, "recover", "defer", "--issue", issue, "--restore-worktree", auth=True)
    if primary is None:
        return f"- An interrupted repair cannot be resumed; set the issue aside: {defer}\n{launch}"
    steps = f"- Record the recovery: {primary}{hint}\n{launch}"
    if classification in ("environment", "runner-defect"):
        return steps
    return steps + f"\n- Or set the issue aside instead: {defer}"


def blocked(ctx, *, issue, classification, event=None, error="", step=None, phase=None, result=None, who="worker",
            draft=None, draft_problem=None, draft_path=None, evidence_paths=()):
    subject = issue or f"Batch {ctx['batch']}"
    values = {"subject": subject, "owner": ctx["owner"], "phase": phase or step or "model", "step": step or "current",
              "error": short_cause(error, 300)}
    known = event in ("worker_blocked", "review_blocked", "checks_failed", "delivery_failed", "budget_exceeded")
    cause = variant("blocked", "cause", event, values) if known else short_cause(error)
    happened = variant("blocked", "happened", event if known else "other", values)
    if event in ("review_blocked", "checks_failed", "delivery_failed"):
        happened += f" The runner reported: {short_cause(error, 300)}."
    needed = variant("blocked", "needed", event if known else classification, values)
    words = own_words(who=who, result=result, draft=draft, draft_problem=draft_problem, draft_path=draft_path) \
        if event in ("worker_blocked", "review_blocked", "checks_failed", "budget_exceeded") else ""
    return render("blocked", {"subject": subject, "owner": ctx["owner"], "cause": cause, "what_happened": happened,
                              "own_words": words, "needed": needed,
                              "continue_steps": recovery_steps(ctx, issue=issue, event=event, step=step, phase=phase,
                                                               classification=classification),
                              "evidence": evidence(*evidence_paths)}, headline=classification)


def deferred(ctx, *, issue, cause, block, result=None, who="worker", draft=None, evidence_paths=()):
    if cause.get("rule_text"):
        why = f"The issue's own decision rule \"{cause['rule_text']}\" matched block {block['id']}."
    else:
        why = f"The batch policy {cause.get('policy', 'on_block')} applies to block {block['id']}."
    why += f" It blocked at the {block.get('step', 'current')} step: {short_cause(block.get('error'), 240)}."
    restore = (f"It is not accepted and keeps its Linear state. Once the batch has stopped:\n"
               f"- Record the restore: {command(ctx, 'recover', 'resume', '--issue', issue, auth=True)}\n"
               f"- Then start it: {command(ctx, 'launch')}")
    return render("deferred", {"issue": issue, "owner": ctx["owner"], "why": why, "restore": restore,
                               "own_words": own_words(who=who, result=result, draft=draft),
                               "evidence": evidence(*evidence_paths)})


RECOVERY_KINDS = ("resume", "review", "budget", "publish", "defer")


def recovery(ctx, *, record, step=None, note=None, evidence_paths=()):
    details = record.get("details", {})
    subject = details.get("issue") or f"batch {ctx['batch']}"
    values = {"subject": subject, "step": step or details.get("step") or "saved", "phase": details.get("phase", "")}
    kind = record["kind"] if record["kind"] in RECOVERY_KINDS else "resume"
    action = variant("recovery", "kind", kind, values)
    if kind == "review" and details.get("redeliver"):
        action += " Delivery is re-run first; the previous packet is kept."
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


def usage_prose(usage):
    totals = (usage or {}).get("totals", {})
    def amount(value):
        return f"{value / 1e6:.1f}M" if value >= 1e6 else f"{value / 1e3:.0f}k" if value >= 1e3 else str(value)
    if not isinstance(totals.get("input_tokens"), int) or not isinstance(totals.get("output_tokens"), int):
        return "Usage telemetry was incomplete; see the terminal report." if usage and usage.get("sessions") else ""
    cached = totals.get("cached_input_tokens")
    return (f"About {amount(totals['input_tokens'])} input tokens"
            + (f" ({amount(cached)} cached)" if isinstance(cached, int) else "")
            + f" and {amount(totals['output_tokens'])} output tokens over {plural(usage.get('sessions', 0), 'model session')}"
            + "; these are counters, not billed cost.")


def batch_finished(ctx, *, outcome, done, total, issues, usage=None, checkpoint=None, deferred=(), evidence_paths=()):
    if outcome == "partial" and deferred:
        steps = (f"- Restore a set-aside issue: {command(ctx, 'recover', 'resume', '--issue', '<issue>', auth=True)}\n"
                 f"- Then start it: {command(ctx, 'launch')}")
    elif outcome in ("checkpoint", "stopped", "partial"):
        steps = f"- After reviewing the state, relaunch: {command(ctx, 'launch', '--clear-stop')}"
    else:
        steps = ""
    return render("batch-finished", {"batch": ctx["batch"], "owner": ctx["owner"], "total": total,
                                     "done_count": len(done), "checkpoint": checkpoint or "", "issues": issues,
                                     "usage": usage_prose(usage), "continue_steps": steps,
                                     "evidence": evidence(*evidence_paths)}, headline=outcome)


def batch_paused(ctx, *, subject, where, issues, evidence_paths=()):
    return render("batch-paused", {"batch": ctx["batch"], "owner": ctx["owner"], "subject": subject, "where": where,
                                   "issues": issues,
                                   "continue_steps": f"Follow the steps in the comment on {where}.",
                                   "evidence": evidence(*evidence_paths)})


def watchdog(ctx, *, condition, subject, minutes=None, observed="", last_update="", evidence_paths=()):
    steps = (f"- Check the state and the supervisor log: {command(ctx, 'status')}\n"
             f"- If nothing is running, record a recovery: {command(ctx, 'recover', 'resume', auth=True)}\n"
             f"- Then start it: {command(ctx, 'launch')}")
    return render("watchdog", {"batch": ctx["batch"], "owner": ctx["owner"], "subject": subject, "minutes": minutes,
                               "observed": observed, "last_update": quote(last_update) if last_update else "",
                               "continue_steps": steps, "evidence": evidence(*evidence_paths)}, headline=condition)


def subject_line(text):
    """A notification subject: the comment's first sentence."""
    return first_sentence(text)[:200]
