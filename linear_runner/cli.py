"""Sequential, allowlisted Linear issue execution through an installed model CLI (Codex or Claude Code).

Deterministic Python owns scheduling, Linear synchronization, checks, commits and
publication. Only implementation, bounded repair and independent review invoke a model.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import subprocess
import sys

from linear_runner.config import (PHASES, ConfigError, load_config, pin_resolution, pinned_config, read_json,
                                  variable_overrides, write_json, write_resolved)
from linear_runner.engine.runner import LockBusy, Runner, now, project_lock
from linear_runner.linear.client import LinearClient


def summarize(config):
    """Offline validation report: no state, credentials, Linear or model CLI access (a Claude
    token file's metadata is checked, its content never read)."""
    from linear_runner.backends.claude import offline_auth_check
    from linear_runner.config import RUNNER_ROOT
    from linear_runner.version import pin_problem
    pinned = config.get("runner_version")
    return {"valid": True, "batch": config["batch_id"], "project": config["project_name"],
            "workspace": config["linear_workspace"], "issues": config["issues"], "state_dir": config["state_dir"],
            "resolution_pending": {"project": config["project_name"], "assignee": config["assignee"]},
            "runner": config["runner"],
            "runner_version": {"pinned": pinned, "problem": pin_problem(config, RUNNER_ROOT)} if pinned else None,
            "supervision": config["supervision"], "attention": config["attention"],
            "launcher": {k: config["launcher"][k] for k in ("backend", "cpu_list", "stop_on_exit")},
            "delivery_integrity": bool(config["delivery_integrity"]), "intake_mode": config["intake_mode"],
            "context_controls": config["context_controls"], "phase_overrides": config.get("phase_overrides", {}),
            "variable_overrides": variable_overrides(config),
            "checks": [dict({"name": c["name"], "tier": c["tier"],
                             "timeout_seconds": c.get("timeout_seconds",
                                                      config["policy"]["phases"]["check_timeout_seconds"])},
                            **({"last_issue": c["last_issue"]} if "last_issue" in c else {}))
                       for c in config["checks"]],
            "claude_auth": offline_auth_check(config),
            "contract": config["contract"], "interface": config.get("_interface"), "layers": config["_layers"]}


class VersionAction(argparse.Action):
    """``--version``: the release, commit and clean/dirty state of this runner checkout."""
    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, help="print the runner release, commit and checkout state")

    def __call__(self, parser, namespace, values, option_string=None):
        from linear_runner.config import RUNNER_ROOT, runner_identity
        from linear_runner.version import describe, tagged
        print(describe(runner_identity(), at_tag=tagged(RUNNER_ROOT)))
        parser.exit()


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--batch", required=True, help="batch file (issue allowlist, gates, project name)")
    common.add_argument("--home", help="private configuration home (default: $LINEAR_RUNNER_HOME, then ~/.config/linear-runner)")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action=VersionAction)
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    for name, text in (("validate-config", "offline validation; no state, Linear or model CLI"),
                       ("dry-run", "resolve names, check gates and select the next issue without dispatch"),
                       ("wait", "block until the launched batch stops, then print how (complete, checkpoint, "
                                "paused); reads the state directory only, no model, no Linear"),
                       ("stop", "write the STOP marker (stops between issues)"),

                       ("clear-stop", "remove the STOP marker")):
        commands.add_parser(name, parents=[common], help=text)
    status = commands.add_parser("status", parents=[common],
                                 help="a short summary of the batch: phase, active issue and step, repairs, done and "
                                      "remaining issues, supervisor, STOP marker, pending recovery")
    status.add_argument("--json", action="store_true",
                        help="the full saved state, supervisor status, launch and watchdog record (as before 2.6.0)")
    watch_progress = commands.add_parser("watch", parents=[common],
                                         help="print one line per change of the active issue, phase, step, repairs or "
                                              "supervisor status; exit as `wait` does when the batch stops")
    watch_progress.add_argument("--timeout", type=float, metavar="SECONDS",
                                help="exit with code 6 when the batch has not stopped by then (start it again)")
    watch_progress.add_argument("--interval", type=float, default=10.0, metavar="SECONDS", help="poll interval (10)")
    run = commands.add_parser("run", parents=[common], help="run in this process (no supervisor)")
    run.add_argument("--max-issues", type=int, default=1)
    run.add_argument("--resume", action="store_true")
    watch = commands.add_parser("watchdog", parents=[common],
                                help="model-free check for a vanished or stalled supervisor (run by the launch timer)")
    watch.add_argument("--launch-id", help="the launch whose timer runs this check (set by launch)")
    watch.add_argument("--timer", help="the timer unit to stop once that launch has an outcome (set by launch)")
    launch = commands.add_parser("launch", parents=[common],
                                 help="preflight (with a tiny start check per model backend), start the supervisor, "
                                      "confirm, exit")
    launch.add_argument("--backend", choices=["systemd-user", "foreground"], help="default: site launcher.backend")
    launch.add_argument("--clear-stop", action="store_true",
                        help="remove an inspected STOP marker after preflight passes (not needed for the marker a "
                             "pending recovery was recorded against)")
    launch.add_argument("--rerun-preflight", action="store_true", help="do not reuse earlier preflight results")
    supervise = commands.add_parser("supervise", parents=[common], help="the supervisor a launched unit runs")
    supervise.add_argument("--launch-id", required=True)
    for sub in (launch, supervise):
        sub.add_argument("--stop-after", action="append", default=[], metavar="ISSUE",
                         help="planned checkpoint: stop after this issue is accepted (repeatable)")
        sub.add_argument("--scope", choices=["queue", "active"], default="queue",
                         help="active: finish only the saved active issue, then stop")
    offline = argparse.ArgumentParser(add_help=False)
    offline.add_argument("--runs", nargs="+", required=True, metavar="DIR",
                         help="artifact roots or run directories to read (copies are deduplicated)")
    offline.add_argument("--issues", nargs="+", metavar="ISSUE", help="only these issues (default: all found)")
    report = commands.add_parser("report", parents=[offline],
                                 help="offline: render trajectory, usage, attempts and validation audit from saved records")
    report.add_argument("--out", help="directory for trajectory.json/.md/.html (default: print Markdown)")
    report.add_argument("--until", help="ignore invocations starting after this ISO time (a recorded capture time)")
    report.add_argument("--group", action="append", default=[], metavar="LABEL=ISSUE,ISSUE",
                        help="batch comparison row (repeatable; default: one row for all issues)")
    report.add_argument("--format", choices=["md", "html", "both"], default="both")
    report.add_argument("--check-trajectory", metavar="JSON", help="recorded trajectory whose summaries must match")
    report.add_argument("--check-comparison", metavar="JSON", help="recorded comparison rows to match")
    report.add_argument("--check-label", help="compare only the recorded comparison row with this batch label")
    measure = commands.add_parser("measure", parents=[offline],
                                  help="offline: intake bytes by component, prompt/tool-output bytes and input growth")
    measure.add_argument("--rollouts", metavar="DIR",
                         help="Codex session-log directory for per-call context growth (read only; default "
                              "$CODEX_HOME/sessions, else ~/.codex/sessions, when it exists)")
    measure.add_argument("--no-rollouts", action="store_true", help="do not read Codex session logs")
    measure.add_argument("--replay-compact", action="store_true",
                         help="also rebuild each saved intake with the compact builder and report its size")
    measure.add_argument("--json", metavar="PATH", help="also write the full measurement as JSON")
    insert = commands.add_parser("insert-issues",
                                 help="edit the batch file: insert issues into its allowlist (before an issue, or at "
                                      "the end), with their implement timeout and a new terminal_issue; adopt the "
                                      "edit with `recover repin-config`")
    insert.add_argument("--batch", required=True, help="batch file, or the id of one in <home>/batches")
    insert.add_argument("--home", help="private configuration home (default: $LINEAR_RUNNER_HOME, then ~/.config/linear-runner)")
    insert.add_argument("--issues", nargs="+", required=True, metavar="ISSUE", help="the issues to insert, in order")
    insert.add_argument("--before", metavar="ISSUE", help="insert before this issue (default: at the end)")
    insert.add_argument("--implement-timeout", type=int, metavar="SECONDS",
                        help="phase_overrides.<issue>.implement.timeout_seconds for each inserted issue")
    insert.add_argument("--terminal-issue", metavar="ISSUE", help="the new terminal_issue")
    template = commands.add_parser("sync-linear-template",
                                   help="render templates/issue-contract.md as the Linear issue template \"Runner "
                                        "issue contract\", check the workspace's copy and record its ID")
    template.add_argument("--home", help="private configuration home (default: $LINEAR_RUNNER_HOME, then ~/.config/linear-runner)")
    template.add_argument("--workspace", help="workspace slug (workspaces/<slug>.json); required unless --dry-run")
    template.add_argument("--team", help="Linear team whose templates to search (default: all the user can see)")
    template.add_argument("--dry-run", action="store_true", help="print the rendered template; no Linear access, no writes")
    recover = commands.add_parser("recover", help="record an authorized recovery for the next launch")
    kinds = recover.add_subparsers(dest="kind", required=True, metavar="kind")
    authority = argparse.ArgumentParser(add_help=False)
    authority.add_argument("--reason", required=True, help="why this recovery is needed (recorded)")
    authority.add_argument("--authorized-by", required=True, help="who authorized it (recorded)")
    then = argparse.ArgumentParser(add_help=False)
    then.add_argument("--then", choices=["continue", "stop"], default="continue",
                      help="after the recovered issue: continue the batch (default) or stop")
    note = argparse.ArgumentParser(add_help=False)
    note.add_argument("--note-file", help="owner note appended to later model prompts (recorded with its hash)")
    resume = kinds.add_parser("resume", parents=[common, authority, then, note], help="continue the active issue from its saved step")
    resume.add_argument("--repin-contract", action="store_true", help="adopt the edited live issue (before acceptance only)")
    resume.add_argument("--issue", help="restore this deferred, parked issue as the active issue")
    review = kinds.add_parser("review", parents=[common, authority, then, note], help="re-run only the independent review")
    review.add_argument("--repin-contract", action="store_true", help="adopt the edited live issue before reviewing")
    review.add_argument("--redeliver", action="store_true", help="re-run delivery first; the previous packet is kept")
    repair = kinds.add_parser("repair", parents=[common, authority, then, note],
                              help="after a blocked review: send the reviewer's findings back to the worker as a repair "
                                   "(next repair slot), then validate, commit on top and review afresh")
    repair.add_argument("--repin-contract", action="store_true",
                        help="adopt the edited live issue (a clarified criterion) for the repair and the fresh review")
    budget = kinds.add_parser("budget", parents=[common, authority, then, note], help="reconcile a soft-budget checkpoint")
    budget.add_argument("--phase", required=True, choices=list(PHASES))
    budget.add_argument("--input-tokens", type=int, required=True)
    budget.add_argument("--output-tokens", type=int, required=True)
    budget.add_argument("--tool-calls", type=int, required=True)
    kinds.add_parser("revalidate", parents=[common, authority, then],
                     help="re-run the checks on the current source at a repair or validate stop (no model, no repair slot)")
    publish = kinds.add_parser("publish", parents=[common, authority, then],
                               help="reconcile publication of an accepted review; no model")
    publish.add_argument("--accept-contract-drift", action="store_true",
                         help="also re-pin the issue contract when only fields outside the accepted criteria and "
                              "scope changed (refused, naming the fields, otherwise)")
    kinds.add_parser("cancel", parents=[common, authority],
                     help="withdraw a pending recovery that was not launched (works even after the configuration changed)")
    repin_config = kinds.add_parser("repin-config", parents=[common, authority],
                                    help="adopt a changed configuration and/or runner commit for a paused or stopped "
                                         "batch (applied at once; record the recovery the state needs afterwards)")
    repin_config.add_argument("--append-issues", action="store_true",
                              help="also adopt issues added at the end of the batch's allowlist (and a new "
                                   "terminal_issue); earlier issues, their order and their history stay as they are")
    repin_config.add_argument("--reorder-unclaimed", action="store_true",
                              help="also adopt issues inserted among, and a new order of, the issues the batch has "
                                   "not claimed; the allowlist up to its last done, active or deferred issue stays")
    defer_issue = kinds.add_parser("defer", parents=[common, authority], help="defer an issue; the queue continues without it")
    defer_issue.add_argument("--issue", required=True)
    defer_issue.add_argument("--restore-worktree", action="store_true",
                             help="park uncommitted work in a Git ref and restore a clean worktree")
    defer_issue.add_argument("--keep-commit", action="store_true",
                             help="continue on top of the deferred issue's unaccepted controller commit")
    return parser


def status_report(config):
    root = Path(config["state_dir"])
    state = read_json(root / "state.json") if (root / "state.json").exists() else {"phase": "not started"}
    supervisor = read_json(root / "supervisor.json") if (root / "supervisor.json").exists() else None
    launch = None
    if supervisor and (root / "launches" / f"{supervisor['launch_id']}.json").exists():
        record = read_json(root / "launches" / f"{supervisor['launch_id']}.json")
        launch = {"launch_id": record["launch_id"], "backend": record["backend"], "unit": record["spec"]["unit"],
                  "launcher": record.get("launcher"), "cleared_stop": record.get("cleared_stop"),
                  "watchdog_timer": record.get("watchdog_timer")}
    watch = read_json(root / "watchdog.json") if (root / "watchdog.json").exists() else {}
    timer = ((launch or {}).get("watchdog_timer") or {}).get("timer")
    watchdog_status = {"timer": timer, "stopped": (watch.get("timers") or {}).get(timer) if timer else None,
                       "alerts": sorted(watch.get("alerts", {})), "needs_input": sorted(watch.get("needs_input") or {})}
    return dict(state, supervisor=supervisor, launch=launch, watchdog=watchdog_status,
                stop_marker=(root / "STOP").read_text().strip() if (root / "STOP").exists() else None)


def status_summary(config, report):
    """``status`` without ``--json`` (W-346): at most 15 lines, no issue description."""
    done = [h["issue_id"] for h in report.get("history", [])]
    deferred = sorted(report.get("deferred") or {})
    remaining = [i for i in config["issues"] if i not in done and i not in deferred]
    active = report.get("active") or {}
    supervisor = report.get("supervisor") or {}
    pending = report.get("pending_recovery") or {}
    snapshot = Path(config["state_dir"]) / "snapshot.json"
    waiting = (read_json(snapshot).get("waiting") or {}) if snapshot.exists() else {}
    lines = [f"Batch {config['batch_id']}: phase {report.get('phase')}",
             (f"Active: {active['issue_id']} at step {active.get('step')}, repairs used {active.get('repairs', 0)}"
              if active else "Active: none"),
             f"Done ({len(done)}): {', '.join(done) or 'none'}",
             f"Remaining ({len(remaining)}): {', '.join(remaining) or 'none'}"]
    if deferred:
        lines.append(f"Deferred: {', '.join(deferred)}")
    if waiting:
        lines.append("Waiting: " + "; ".join(f"{i} on {', '.join(b)}" for i, b in waiting.items()))
    lines.append(f"Supervisor: {supervisor.get('status', 'not launched')}"
                 + (f", outcome {supervisor['outcome']}" if supervisor.get("outcome") else "")
                 + (f" (launch {supervisor['launch_id']})" if supervisor.get("launch_id") else ""))
    if report.get("error"):
        lines.append(f"Error: {str(report['error'])[:300]}")
    if report.get("stop_marker"):
        lines.append(f"STOP marker: {report['stop_marker'][:300]}")
    if pending:
        lines.append(f"Pending recovery: {pending.get('id')} ({pending.get('kind')})")
    lines.append("Full record: status --json")
    return lines


def stop_watchdog_timer(root, run=subprocess.run):
    """``stop``: stop the latest launch's watchdog timer now if no supervisor is running;
    otherwise leave it watching until the supervisor exits (it then stops itself)."""
    from linear_runner.supervision import watchdog
    status = read_json(root / "supervisor.json") if (root / "supervisor.json").exists() else None
    record_path = root / "launches" / f"{(status or {}).get('launch_id')}.json"
    timer = (read_json(record_path).get("watchdog_timer") or {}).get("timer") if status and record_path.exists() else None
    if not timer:
        return None
    if watchdog.timer_stopped(root, timer):
        return {"timer": timer, "state": "already stopped"}
    if status.get("status") == "running" and watchdog.pid_alive(status.get("pid")):
        return {"timer": timer, "state": "left running until the supervisor exits between issues"}
    watchdog.record_timer_stop(root, timer, "runner.py stop", watchdog.systemctl_stop(timer, run))
    return {"timer": timer, "state": "stopped"}


def mention_warnings(args):
    """Warnings for recovery text the recovery comment will quote that names an issue."""
    from linear_runner.linear import messages
    if args.kind not in messages.RECOVERY_KINDS:
        return []  # cancel and repin-config post no recovery comment
    texts = [(args.reason, "reason")]
    note = getattr(args, "note_file", None)
    if note and Path(note).expanduser().is_file():
        texts.append((Path(note).expanduser().read_text(), "note"))
    return [w for w in (messages.mention_warning(text, what) for text, what in texts) if w]


def recover(args, runner):
    from linear_runner.supervision import recovery
    for warning in mention_warnings(args):
        print(warning, file=sys.stderr)
    common = {"reason": args.reason, "authorized_by": args.authorized_by}
    if args.kind == "resume":
        return recovery.recover_resume(runner, then=args.then, note_file=args.note_file, repin=args.repin_contract,
                                       issue=args.issue, **common)
    if args.kind == "review":
        return recovery.recover_review(runner, then=args.then, note_file=args.note_file, repin=args.repin_contract,
                                       redeliver=args.redeliver, **common)
    if args.kind == "repair":
        return recovery.recover_repair(runner, then=args.then, note_file=args.note_file, repin=args.repin_contract,
                                       **common)
    if args.kind == "budget":
        limits = {"input_tokens": args.input_tokens, "output_tokens": args.output_tokens, "tool_calls": args.tool_calls}
        return recovery.recover_budget(runner, phase=args.phase, limits=limits, then=args.then,
                                       note_file=args.note_file, **common)
    if args.kind == "revalidate":
        return recovery.recover_revalidate(runner, then=args.then, **common)
    if args.kind == "publish":
        return recovery.recover_publish(runner, then=args.then, accept_drift=args.accept_contract_drift, **common)
    if args.kind == "cancel":
        return recovery.recover_cancel(runner, **common)
    return recovery.recover_defer(runner, issue=args.issue, restore_worktree=args.restore_worktree,
                                  keep_commit=args.keep_commit, **common)


def insert_issues(parser, args):
    """``insert-issues`` (W-343): edit the batch file only. The edited file must still load;
    otherwise the original is restored. Adopting it is `recover repin-config`."""
    from linear_runner.config import find_home, resolve_batch
    home = find_home(args.home)
    try:
        path = resolve_batch(args.batch, home)
    except ConfigError as error:
        parser.error(str(error))
    original = path.read_text()
    batch = json.loads(original)
    issues = list(batch.get("issues", []))
    present = [issue for issue in args.issues if issue in issues]
    if present or len(set(args.issues)) != len(args.issues):
        parser.error(f"insert-issues: {', '.join(present) or 'an issue'} is already in the allowlist or named twice")
    if args.before and args.before not in issues:
        parser.error(f"insert-issues: --before {args.before} is not in the allowlist")
    at = issues.index(args.before) if args.before else len(issues)
    batch["issues"] = issues[:at] + list(args.issues) + issues[at:]
    if args.terminal_issue and args.terminal_issue not in batch["issues"]:
        parser.error(f"insert-issues: --terminal-issue {args.terminal_issue} is not in the allowlist")
    if args.implement_timeout is not None:
        overrides = batch.setdefault("phase_overrides", {})
        for issue in args.issues:
            overrides.setdefault(issue, {}).setdefault("implement", {})["timeout_seconds"] = args.implement_timeout
    if args.terminal_issue:
        batch["terminal_issue"] = args.terminal_issue
    path.write_text(json.dumps(batch, indent=2, ensure_ascii=False) + "\n")
    try:
        load_config(path, home)
    except (ConfigError, OSError) as error:
        path.write_text(original)
        parser.error(f"insert-issues: the edited batch file does not load ({error}); it was left unchanged")
    flag = "--append-issues" if at == len(issues) else "--reorder-unclaimed"
    print(json.dumps({"batch_file": str(path), "issues": batch["issues"], "terminal_issue": batch.get("terminal_issue"),
                      "next": f"python3 runner.py recover repin-config {flag} --batch {args.batch} --reason R "
                              "--authorized-by A"}, indent=2, ensure_ascii=False))


def offline_report(args):
    """``report``: model-free trajectory/usage rendering from saved records; no config or Linear."""
    from linear_runner.reporting import trajectory
    groups = {}
    for value in args.group:
        label, _, members = value.partition("=")
        groups[label.strip()] = [m.strip() for m in members.split(",") if m.strip()]
    result = trajectory.from_roots(args.runs, issues=args.issues, until=args.until, groups=groups or None,
                                   captured_at=now())
    reproduction = None
    if args.check_trajectory or args.check_comparison:
        reproduction = trajectory.compare_recorded(
            result, read_json(args.check_trajectory) if args.check_trajectory else None,
            read_json(args.check_comparison) if args.check_comparison else None, args.check_label)
    if not args.out:
        print(trajectory.render_markdown(result, reproduction))
        return
    formats = ("md", "html") if args.format == "both" else (args.format,)
    paths = trajectory.write(args.out, result, formats=formats, reproduction=reproduction)
    print(json.dumps({"written": paths, "issues": result["issues"], "pending": len(result["pending"]),
                      "reproduction": None if reproduction is None else
                      {"matched": sum(r["match"] for r in reproduction), "compared": len(reproduction)}}, indent=2))


def offline_measure(args):
    """``measure``: model-free context-cost measurement from saved records."""
    from linear_runner.reporting import measure
    compact = None
    if args.replay_compact:
        from linear_runner.engine import intake
        compact = intake.compact_from_saved
    status = measure.rollout_location(args.rollouts, disabled=args.no_rollouts)
    if status["note"]:
        print(status["note"], file=sys.stderr)
    result = measure.measure(args.runs, issues=args.issues, rollouts=status["location"] if status["found"] else None,
                             compact=compact, rollout_status=status)
    if args.json:
        write_json(Path(args.json), result)
    print(measure.render_markdown(result))


def sync_linear_template(parser, args):
    from linear_runner.config import RUNNER_ROOT, _path, find_home, load_registry, read_layer
    from linear_runner.linear import template
    from linear_runner.version import RELEASE
    home = find_home(args.home)
    try:
        policy, _, _ = load_registry(home)
    except (ConfigError, OSError) as error:
        parser.error(str(error))
    rendered = template.render(policy["labels"], RELEASE)
    if args.dry_run:
        print(template.text_of(rendered), end="")
        return
    if not args.workspace:
        parser.error("--workspace is required unless --dry-run")
    path = home / "workspaces" / f"{args.workspace}.json"
    try:
        workspace = read_layer(path, "workspace", f"workspace {args.workspace}")
    except (ConfigError, OSError) as error:
        parser.error(str(error))
    auth = dict(workspace["auth"])
    if auth.get("credentials_file"):  # resolved as load_config does
        auth["credentials_file"] = str(_path(auth["credentials_file"], path.parent,
                                             {"home": str(home), "runner_root": str(RUNNER_ROOT)},
                                             f"workspace {args.workspace}.auth.credentials_file"))
    try:
        report = template.sync(LinearClient(auth), path, rendered, team=args.team)
    except RuntimeError as error:
        parser.error(str(error))
    print(json.dumps(report, indent=2))
    if report["status"] != "current":
        raise SystemExit(1)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "report":
        return offline_report(args)
    if args.command == "measure":
        return offline_measure(args)
    if args.command == "sync-linear-template":
        return sync_linear_template(parser, args)
    if args.command == "insert-issues":
        return insert_issues(parser, args)
    try:
        config = load_config(args.batch, args.home)
    except (ConfigError, OSError) as error:
        parser.error(str(error))
    if args.command == "validate-config":
        try:
            report = summarize(config)
        except RuntimeError as error:
            parser.error(str(error))
        print(json.dumps(report, indent=2))
        return
    root = Path(config["state_dir"])
    if args.command == "status":
        report = status_report(config)
        print(json.dumps(report, indent=2) if args.json else "\n".join(status_summary(config, report)))
        return
    if args.command == "watch":
        from linear_runner.supervision import wait
        try:
            report = wait.watch(root, out=lambda line: print(line, flush=True), poll_seconds=args.interval,
                                timeout=args.timeout)
        except wait.NotLaunched as error:
            parser.error(str(error))
        if report is None:
            print(json.dumps({"outcome": "timeout", "timeout_seconds": args.timeout}))
            raise SystemExit(wait.WATCH_TIMEOUT_EXIT)
        print(json.dumps(report))
        if wait.EXIT_CODES[report["outcome"]]:
            raise SystemExit(wait.EXIT_CODES[report["outcome"]])
        return
    if args.command == "wait":
        from linear_runner.supervision import wait
        try:
            report = wait.wait(root)
        except wait.NotLaunched as error:
            parser.error(str(error))
        print(json.dumps(report, indent=2))
        if wait.EXIT_CODES[report["outcome"]]:
            raise SystemExit(wait.EXIT_CODES[report["outcome"]])
        return
    if args.command == "watchdog":
        from linear_runner.supervision import watchdog
        if not (root / "resolved-config.json").exists():
            print(json.dumps({"status": "idle", "reason": "this batch has not been launched"}))
            return
        try:
            config, _ = pin_resolution(config, None)
        except (ConfigError, RuntimeError, OSError) as error:
            parser.error(str(error))
        print(json.dumps(watchdog.check(config, LinearClient(config["linear"]), launch_id=args.launch_id,
                                        timer=args.timer), indent=2))
        return
    if args.command in ("stop", "clear-stop"):
        root.mkdir(parents=True, exist_ok=True)
        marker = root / "STOP"
        if args.command == "clear-stop":
            marker.unlink(missing_ok=True)
        elif not marker.exists():
            # A reason for the launch refusal to show; an existing marker keeps its own.
            marker.write_text(f"Stop requested with `runner.py stop` at {now()}.\n")
        if args.command == "stop":
            print(json.dumps({"stop_marker": str(marker), "watchdog_timer": stop_watchdog_timer(root)}, indent=2))
        return
    if args.command == "run" and (args.max_issues < 1 or args.max_issues > len(config["issues"]) + 1):
        parser.error("max-issues must be between 1 and the issue count plus one completion check")
    root.mkdir(parents=True, exist_ok=True)
    linear = LinearClient(config["linear"])
    if args.command in ("launch", "supervise", "recover"):
        return supervised_command(parser, args, config, linear)
    with project_lock(root / "controller.lock"):
        # Resolution/configuration/legacy-state errors must not rewrite state or notify Linear.
        try:
            config, fresh = pin_resolution(config, linear)
            runner = Runner(config, linear)
            runner.verify_config()
        except (ConfigError, RuntimeError, OSError) as error:
            parser.error(str(error))
        if fresh:
            write_resolved(config)

        def interrupted(signum, frame):
            runner.stop_child()
            raise KeyboardInterrupt(f"Signal {signum}")
        signal.signal(signal.SIGTERM, interrupted)
        try:
            runner.execute(dry_run=args.command == "dry-run", limit=args.max_issues if args.command == "run" else 1,
                           resume=getattr(args, "resume", False))
        except (Exception, KeyboardInterrupt) as error:
            runner.stop_child()
            runner.log(f"Paused: {error}")
            if args.command != "dry-run":
                runner.report_pause(error)
            raise SystemExit(1)


# A supervisor that just paused may still be posting its stop comments under the lock.
RECOVER_LOCK_WAIT_SECONDS = 60


def locked_recovery(parser, root, action, wait_seconds=None):
    """Run a recovery under the project lock, waiting up to RECOVER_LOCK_WAIT_SECONDS for it.
    A lock still held then ends with one line (exit 2), no traceback; refusals as before."""
    from linear_runner.supervision import recovery
    wait = RECOVER_LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds
    try:
        with project_lock(root / "controller.lock", wait_seconds=wait):
            try:
                record = action()
            except (recovery.RecoveryError, ConfigError, RuntimeError, OSError) as error:
                parser.error(str(error))
    except LockBusy as error:
        print(f"{parser.prog} recover: {error}. The supervisor may still be exiting (posting its stop comments); "
              "wait until `status` shows it exited, then run the same recover command again.", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(record, indent=2))


def supervised_command(parser, args, config, linear):
    """launch / supervise / recover. Refusals exit 2 without writing to Linear."""
    from linear_runner.supervision import launcher
    from linear_runner.supervision import recovery
    from linear_runner.supervision import supervisor
    root = Path(config["state_dir"])
    if args.command == "recover" and args.kind == "repin-config":
        # The one recovery that runs against a configuration that differs from the pinned one.
        return locked_recovery(parser, root, lambda: recovery.recover_repin_config(
            config, linear, reason=args.reason, authorized_by=args.authorized_by, append_issues=args.append_issues,
            reorder_unclaimed=args.reorder_unclaimed))
    if args.command == "recover" and args.kind == "cancel":
        # Withdrawing a pending record changes no work: it runs against the pinned configuration,
        # so a configuration edited since pinning never blocks it (then `repin-config` adopts it).
        return locked_recovery(parser, root, lambda: recover(args, Runner(pinned_config(config), linear)))
    try:
        config, fresh = pin_resolution(config, linear)
    except (ConfigError, RuntimeError, OSError) as error:
        parser.error(str(error))
    if args.command == "recover":
        if fresh:
            parser.error("This batch has no pinned state to recover")
        return locked_recovery(parser, root, lambda: recover(args, Runner(config, linear)))
    if args.command == "supervise":
        if fresh:
            parser.error("supervise needs the pinned configuration written by launch")
        try:
            supervisor.supervise(config, linear, launch_id=args.launch_id, stop_after=args.stop_after,
                                 scope=args.scope, install_signals=True)
        except supervisor.SupervisorRefused as error:
            parser.error(str(error))
        except (Exception, KeyboardInterrupt):
            raise SystemExit(1)
        return
    if fresh:
        write_resolved(config)
    name = args.backend or config["launcher"]["backend"]
    backend = launcher.backend_for(name, supervise=lambda spec: supervisor.supervise(
        config, linear, launch_id=spec["launch_id"], stop_after=spec["stop_after"], scope=spec["scope"],
        install_signals=True))
    try:
        entry = launcher.launch(config, linear, backend=backend, stop_after=args.stop_after, scope=args.scope,
                                clear_stop=args.clear_stop, force_preflight=args.rerun_preflight)
    except (launcher.LaunchError, ConfigError, RuntimeError, OSError) as error:
        parser.error(str(error))
    if entry["started"].get("exit_code"):
        raise SystemExit(1)
