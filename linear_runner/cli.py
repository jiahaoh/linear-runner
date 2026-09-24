"""Sequential, allowlisted Linear issue execution through the installed Codex CLI.

Deterministic Python owns scheduling, Linear synchronization, checks, commits and
publication. Only implementation, bounded repair and independent review invoke Codex.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import subprocess
import sys

from linear_runner.config import (PHASES, ConfigError, load_config, pin_resolution, read_json, write_json,
                                  write_resolved)
from linear_runner.engine.runner import Runner, now, project_lock
from linear_runner.linear.client import LinearClient


def summarize(config):
    """Offline validation report: no state, credentials, Linear or Codex access."""
    return {"valid": True, "batch": config["batch_id"], "project": config["project_name"],
            "workspace": config["linear_workspace"], "issues": config["issues"], "state_dir": config["state_dir"],
            "resolution_pending": {"project": config["project_name"], "assignee": config["assignee"]},
            "runner": config["runner"], "supervision": config["supervision"], "attention": config["attention"],
            "launcher": {k: config["launcher"][k] for k in ("backend", "cpu_list", "stop_on_exit")},
            "delivery_integrity": bool(config["delivery_integrity"]), "intake_mode": config["intake_mode"],
            "context_controls": config["context_controls"],
            "contract": config["contract"], "layers": config["_layers"]}


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--batch", required=True, help="batch file (issue allowlist, gates, project name)")
    common.add_argument("--home", help="private configuration home (default: $LINEAR_RUNNER_HOME, then ~/.config/linear-runner)")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    for name, text in (("validate-config", "offline validation; no state, Linear or Codex"),
                       ("dry-run", "resolve names, check gates and select the next issue without dispatch"),
                       ("status", "print saved state, supervisor status and pending recovery"),
                       ("stop", "write the STOP marker (stops between issues)"),

                       ("clear-stop", "remove the STOP marker")):
        commands.add_parser(name, parents=[common], help=text)
    run = commands.add_parser("run", parents=[common], help="run in this process (no supervisor)")
    run.add_argument("--max-issues", type=int, default=1)
    run.add_argument("--resume", action="store_true")
    watch = commands.add_parser("watchdog", parents=[common],
                                help="model-free check for a vanished or stalled supervisor (run by the launch timer)")
    watch.add_argument("--launch-id", help="the launch whose timer runs this check (set by launch)")
    watch.add_argument("--timer", help="the timer unit to stop once that launch has an outcome (set by launch)")
    launch = commands.add_parser("launch", parents=[common], help="model-free preflight, start the supervisor, confirm, exit")
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
    budget = kinds.add_parser("budget", parents=[common, authority, then, note], help="reconcile a soft-budget checkpoint")
    budget.add_argument("--phase", required=True, choices=list(PHASES))
    budget.add_argument("--input-tokens", type=int, required=True)
    budget.add_argument("--output-tokens", type=int, required=True)
    budget.add_argument("--tool-calls", type=int, required=True)
    kinds.add_parser("revalidate", parents=[common, authority, then],
                     help="re-run the checks on the current source at a repair or validate stop (no model, no repair slot)")
    kinds.add_parser("publish", parents=[common, authority, then], help="reconcile publication of an accepted review; no model")
    kinds.add_parser("cancel", parents=[common, authority], help="withdraw a pending recovery that was not launched")
    kinds.add_parser("repin-config", parents=[common, authority],
                     help="adopt a changed configuration and/or runner commit for a paused or stopped batch "
                          "(applied at once; record the recovery the state needs afterwards)")
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


def recover(args, runner):
    from linear_runner.supervision import recovery
    common = {"reason": args.reason, "authorized_by": args.authorized_by}
    if args.kind == "resume":
        return recovery.recover_resume(runner, then=args.then, note_file=args.note_file, repin=args.repin_contract,
                                       issue=args.issue, **common)
    if args.kind == "review":
        return recovery.recover_review(runner, then=args.then, note_file=args.note_file, repin=args.repin_contract,
                                       redeliver=args.redeliver, **common)
    if args.kind == "budget":
        limits = {"input_tokens": args.input_tokens, "output_tokens": args.output_tokens, "tool_calls": args.tool_calls}
        return recovery.recover_budget(runner, phase=args.phase, limits=limits, then=args.then,
                                       note_file=args.note_file, **common)
    if args.kind == "revalidate":
        return recovery.recover_revalidate(runner, then=args.then, **common)
    if args.kind == "publish":
        return recovery.recover_publish(runner, then=args.then, **common)
    if args.kind == "cancel":
        return recovery.recover_cancel(runner, **common)
    return recovery.recover_defer(runner, issue=args.issue, restore_worktree=args.restore_worktree,
                                  keep_commit=args.keep_commit, **common)


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


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "report":
        return offline_report(args)
    if args.command == "measure":
        return offline_measure(args)
    try:
        config = load_config(args.batch, args.home)
    except (ConfigError, OSError) as error:
        parser.error(str(error))
    if args.command == "validate-config":
        print(json.dumps(summarize(config), indent=2))
        return
    root = Path(config["state_dir"])
    if args.command == "status":
        print(json.dumps(status_report(config), indent=2))
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
        marker.touch() if args.command == "stop" else marker.unlink(missing_ok=True)
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


def supervised_command(parser, args, config, linear):
    """launch / supervise / recover. Refusals exit 2 without writing to Linear."""
    from linear_runner.supervision import launcher
    from linear_runner.supervision import recovery
    from linear_runner.supervision import supervisor
    root = Path(config["state_dir"])
    if args.command == "recover" and args.kind == "repin-config":
        # The one recovery that runs against a configuration that differs from the pinned one.
        with project_lock(root / "controller.lock"):
            try:
                record = recovery.recover_repin_config(config, linear, reason=args.reason,
                                                       authorized_by=args.authorized_by)
            except (recovery.RecoveryError, ConfigError, RuntimeError, OSError) as error:
                parser.error(str(error))
        print(json.dumps(record, indent=2))
        return
    try:
        config, fresh = pin_resolution(config, linear)
    except (ConfigError, RuntimeError, OSError) as error:
        parser.error(str(error))
    if args.command == "recover":
        if fresh:
            parser.error("This batch has no pinned state to recover")
        with project_lock(root / "controller.lock"):
            try:
                record = recover(args, Runner(config, linear))
            except (recovery.RecoveryError, ConfigError, RuntimeError, OSError) as error:
                parser.error(str(error))
        print(json.dumps(record, indent=2))
        return
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
