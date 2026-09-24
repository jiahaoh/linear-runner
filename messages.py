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
    from config import DEFAULT_HOME, batch_argument, find_home
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


def draft_post(text, who, phase):
    """A linted model draft as posted: unchanged, with a one-line attribution."""
    return text.strip() + f"\n\n_Written by the {who} ({phase} phase); posted by the runner._"


# --- Runner-authored events -------------------------------------------------------------

def claim(ctx, *, issue, selection, check_count, criteria_count, run_dir):
    return render("claim", {"issue": issue, "profile": selection["profile"], "model": selection["model"],
                            "effort": selection["effort"], "check_count": plural(check_count, "configured check"),
                            "criteria_count": criteria_phrase(criteria_count), "evidence": evidence(run_dir)})


def ready(ctx, *, issue, result, attempt, draft_problem=None, deliverables=(), missing=()):
    entries = result.get("acceptance", [])
    met = sum(e.get("satisfied") is True for e in entries if isinstance(e, dict))
    noun = "criterion" if len(entries) == 1 else "criteria"
    criteria = f"The worker marked {met} of {len(entries)} acceptance {noun} as met; the runner now runs the checks."
    if draft_problem:
        criteria += f" The worker's own ready note was not posted because {draft_problem}."
    limits = [plain(item, 200) for item in result.get("limitations", []) if str(item).strip()]
    if missing:
        criteria += f" Listed deliverables that were not found: {listing(missing)}."
    return render("ready", {"issue": issue, "summary": quote(result.get("summary")), "criteria": criteria,
                            "deliverables": deliverable_lines(deliverables),
                            "limitations": " ".join(l if l.endswith(".") else l + "." for l in limits),
                            "evidence": evidence(attempt)})


def validation(ctx, *, issue, records, passed, repair=None, directory=None):
    from delivery import check_passed
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
    return render("validation", fields, headline="passed" if passed else "repairing")


def review(ctx, *, issue, result, attempt, draft_problem=None):
    count = len(result.get("acceptance", []))
    summary = quote(result.get("summary"))
    if draft_problem:
        summary += f"\n\nThe reviewer's note did not follow its template ({draft_problem}), so it is quoted here."
    return render("review", {"issue": issue, "criteria": criteria_phrase(count), "summary": summary,
                             "evidence": evidence(attempt)}, headline="accepted")


def done(ctx, *, issue, commit, criteria_count, repairs, run_dir, deliverables=()):
    note = "" if not repairs else f"It needed {plural(repairs, 'repair')} before the checks passed."
    return render("done", {"issue": issue, "criteria": criteria_phrase(criteria_count), "commit": commit[:12],
                           "branch": ctx["branch"], "repairs": note, "deliverables": deliverable_lines(deliverables),
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
        primary = ("Record a publish-only recovery (no model runs):", command(ctx, "recover", "publish", auth=True))
    else:
        primary = ("Record the recovery (you can add --note-file with a note for the worker):",
                   command(ctx, "recover", "resume", auth=True))
    if classification in ("environment", "runner-defect"):
        return blocks(primary, launch)
    return blocks(primary, launch, aside)


def blocked(ctx, *, issue, classification, event=None, error="", step=None, phase=None, result=None, who="worker",
            draft=None, draft_problem=None, draft_path=None, evidence_paths=(), repairs=None):
    subject = issue or f"Batch {ctx['batch']}"
    values = {"subject": subject, "phase": phase or step or "model", "step": step or "current",
              "error": short_cause(error, 300)}
    known = event in ("worker_blocked", "review_blocked", "checks_failed", "delivery_failed", "budget_exceeded")
    cause = variant("blocked", "cause", event, values) if known else short_cause(error)
    happened = variant("blocked", "happened", event if known else "other", values)
    if event in ("review_blocked", "checks_failed", "delivery_failed"):
        happened += f" The runner reported: {short_cause(error, 300)}."
    needed = variant("blocked", "needed", "repair_blocked" if event == "worker_blocked" and step == "repair"
                     else event if known else classification, values)
    words = own_words(who=who, result=result, draft=draft, draft_problem=draft_problem, draft_path=draft_path) \
        if event in ("worker_blocked", "review_blocked", "checks_failed", "budget_exceeded") else ""
    return render("blocked", {"subject": subject, "mention": ctx["mention"], "cause": cause, "what_happened": happened,
                              "own_words": words, "needed": needed,
                              "continue_steps": recovery_steps(ctx, issue=issue, event=event, step=step, phase=phase,
                                                               classification=classification, repairs=repairs),
                              "evidence": evidence(*evidence_paths)}, headline=classification)


def deferred(ctx, *, issue, cause, block, result=None, who="worker", draft=None, evidence_paths=()):
    if cause.get("rule_text"):
        why = f"The issue's own decision rule \"{cause['rule_text']}\" matched block {block['id']}."
    else:
        why = f"The batch policy {cause.get('policy', 'on_block')} applies to block {block['id']}."
    why += f" It blocked at the {block.get('step', 'current')} step: {short_cause(block.get('error'), 240)}."
    restore = ("It is not accepted and keeps its Linear state.\n\n" + blocks(
        ("Once the batch has stopped, record the restore:", command(ctx, "recover", "resume", "--issue", issue, auth=True)),
        ("Then start the batch again:", command(ctx, "launch"))))
    return render("deferred", {"issue": issue, "why": why, "restore": restore,
                               "own_words": own_words(who=who, result=result, draft=draft),
                               "evidence": evidence(*evidence_paths)})


RECOVERY_KINDS = ("resume", "revalidate", "review", "budget", "publish", "defer")


def recovery(ctx, *, record, step=None, note=None, evidence_paths=()):
    details = record.get("details", {})
    subject = details.get("issue") or f"batch {ctx['batch']}"
    values = {"subject": subject, "step": step or details.get("step") or "saved", "phase": details.get("phase", "")}
    kind = record["kind"] if record["kind"] in RECOVERY_KINDS else "resume"
    action = variant("recovery", "kind", kind, values)
    if kind == "review" and details.get("redeliver"):
        action += " Delivery is re-run first; the previous packet is kept."
    if kind == "resume" and details.get("repair_retry"):
        action += " The worker gets one more repair of the failing checks, with the owner's note."
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
        steps = blocks(("Restore a set-aside issue:", command(ctx, "recover", "resume", "--issue", "<issue>", auth=True)),
                       ("Then start the batch again:", command(ctx, "launch")))
    elif outcome in ("checkpoint", "stopped", "partial"):
        steps = block("After reviewing the state, relaunch the batch:", command(ctx, "launch", "--clear-stop"))
    else:
        steps = ""
    return render("batch-finished", {"batch": ctx["batch"], "total": total,
                                     "mention": ctx["mention"] if outcome != "complete" else "",
                                     "done_count": len(done), "checkpoint": checkpoint or "", "issues": issues,
                                     "usage": usage_prose(usage), "continue_steps": steps,
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
