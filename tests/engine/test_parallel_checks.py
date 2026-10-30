"""Checks that run at the same time (W-746): the pool, its records and evidence, stop and recovery,
per-check directories and which CPU each caller gets (real Git and processes, fake Linear and models)."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from linear_runner.config import RESOLVED_NAME, live_child, load_config, pin_resolution
from linear_runner.engine.intake import build as build_packet
from linear_runner.engine.runner import CHECK_RUN_KEYS, Runner, fingerprint, git, stop_process_groups, write_json
from linear_runner.supervision import launcher, recovery
from linear_runner.supervision.recovery import RecoveryError
from linear_runner.supervision.supervisor import Supervisor, SupervisorRefused
from tests.fixtures import FakeLinear, make_home, make_repo

# Records its name, CPU argument and times, then sleeps; argv: path name cpu seconds exit-code.
FAKE_CHECK = """
import json, sys, time
path, name, cpu, seconds, code = sys.argv[1:6]
start = time.time()
time.sleep(float(seconds))
with open(path, "a") as out:
    out.write(json.dumps({"name": name, "cpu": cpu, "start": start, "end": time.time()}) + "\\n")
sys.exit(int(code))
"""
# A check whose process leaves a child in its group; both write their PID, and the child ignores SIGTERM.
FAMILY = """
import os, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c",
                          "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"])
with open(sys.argv[1], "a") as out:
    out.write(f"{os.getpid()}\\n{child.pid}\\n")
time.sleep(600)
"""


def dead_or_zombie(pid, within=5):
    """Whether ``pid`` stops running within ``within`` seconds: SIGKILL is not instant, and a child of a
    killed leader may wait for its reaper."""
    deadline = time.monotonic() + within
    while True:
        try:
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return True
        except OSError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        make_repo(self.repo, {"README.md": "fixture", "lib.txt": "one"})
        self.base = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "lib.txt").write_text("two")
        git(self.repo, "add", "--all"); git(self.repo, "commit", "-qm", "change")
        self.trace = self.root / "trace.jsonl"
        self.script = self.root / "fake_check.py"
        self.script.write_text(FAKE_CHECK)
        self.linear = FakeLinear()
        self.count = 0

    def check(self, name, seconds=0.0, code=0, tier="extended", **options):
        return dict({"name": name, "kind": "code", "tier": tier, "inputs": ["lib.txt"], "cwd": ".",
                     "command": ["${python}", str(self.script), str(self.trace), name, "${check_cpu}",
                                 str(seconds), str(code)]}, **options)

    def runner(self, checks, cpus="1,2,3", *, project=None, site=None, cpu_list="0", policy_timeout=None):
        variables = dict({"check_cpus": cpus} if cpus else {}, scratch=str(self.root / "scratch"))
        layers = {"variables": variables, **(site or {})}
        if cpu_list is not None:
            layers["launcher"] = {"cpu_list": cpu_list}
        home, batch = make_home(self.root, self.repo, site=layers, project=dict({"checks": checks}, **(project or {})))
        config, _ = pin_resolution(load_config(batch, home), self.linear)
        return Runner(config, self.linear)

    def validate(self, runner, *, expect=True):
        self.count += 1
        run = self.root / "runs" / f"run-{self.count}"
        run.mkdir(parents=True)
        active = {"run_dir": str(run), "issue_id": "DEV-1", "starting_commit": self.base}
        self.assertEqual(runner.run_checks(active), expect)
        return {r["name"]: r for r in json.loads((Path(active["validation_dir"]) / "checks.json").read_text())}, active

    def traced(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []


class PoolTests(Harness):
    def test_at_most_one_check_per_cpu_and_a_free_cpu_is_used_at_once(self):
        runner = self.runner([self.check("quick", tier="default"), *[self.check(f"c{i}", 0.6) for i in range(5)]])
        records, _ = self.validate(runner)
        trace = [t for t in self.traced() if t["name"] != "quick"]
        self.assertEqual(sorted(t["name"] for t in trace), [f"c{i}" for i in range(5)])
        self.assertEqual({t["cpu"] for t in trace}, {"1", "2", "3"})
        moments = sorted({t["start"] for t in trace})
        most = max(sum(t["start"] <= m < t["end"] for t in trace) for m in moments)
        self.assertEqual(most, 3)  # at most three at once, and all three were used
        for cpu in "123":
            spans = sorted((t["start"], t["end"]) for t in trace if t["cpu"] == cpu)
            for (_, end), (start, _) in zip(spans, spans[1:]):
                self.assertLessEqual(end, start, f"two checks on CPU {cpu} at once")
        for t in trace:
            self.assertEqual(records[t["name"]]["cpu"], t["cpu"])
        # Less than the five in sequence (3 s): a later check started when a CPU came free.
        self.assertLess(max(t["end"] for t in trace) - min(t["start"] for t in trace), 2.4)

    def test_without_a_list_the_checks_run_one_at_a_time_in_shortest_first_order(self):
        checks = [self.check("quick", tier="default"), self.check("a", 0.2), self.check("b", 0.2)]
        records, _ = self.validate(self.runner(checks, cpus=None))
        trace = self.traced()
        self.assertEqual([t["name"] for t in trace], ["quick", "a", "b"])
        self.assertLessEqual(trace[1]["end"], trace[2]["start"])
        self.assertEqual({t["cpu"] for t in trace}, {"0"})  # launcher.cpu_list
        self.assertNotIn("cpu", records["a"])

    def test_one_cpu_and_several_give_the_same_records_and_keys(self):
        def run(cpus):
            self.trace.unlink(missing_ok=True)
            runner = self.runner(checks, cpus=cpus)
            (runner.root / "check-cache.json").unlink(missing_ok=True)  # the same key would reuse the evidence
            return self.validate(runner)[0]
        checks = [self.check("quick", tier="default"), self.check("a", 0.1), self.check("b"), self.check("c", code=0)]
        one, three = run("1"), run("1,2,3")
        self.assertEqual(list(one), ["quick", "a", "b", "c"])
        self.assertEqual(list(three), ["quick", "a", "b", "c"])  # configuration order

        def plain(record):
            times = ("started_at", "finished_at", "cpu", "log", "sha256", "command")
            return {k: v for k, v in record.items() if k not in times}
        for name in one:
            self.assertEqual(plain(one[name]), plain(three[name]), name)
        self.assertEqual([r.get("reused") for r in three.values()], [False] * 4)

    def test_the_longest_runs_first_and_a_check_with_no_record_before_it(self):
        checks = [self.check("quick", tier="default"), self.check("short"), self.check("long"), self.check("fresh"),
                  self.check("failed")]
        runner = self.runner(checks, cpus="1")

        def passing(seconds):
            return {"status": "passed", "exit_code": 0, "key": "stale", "started_at": "2026-10-22T00:00:00+00:00",
                    "finished_at": f"2026-10-22T00:00:{seconds:02d}+00:00"}
        write_json(runner.root / "check-cache.json",
                   {"short": passing(5), "long": passing(50), "failed": dict(passing(1), status="failed", exit_code=1)})
        self.validate(runner)
        self.assertEqual([t["name"] for t in self.traced()], ["quick", "fresh", "failed", "long", "short"])

    def test_an_extended_failure_stops_nothing(self):
        checks = [self.check("quick", tier="default"), self.check("bad", 0.0, code=3), self.check("good", 0.5)]
        records, active = self.validate(self.runner(checks), expect=False)
        self.assertEqual(records["bad"]["status"], "failed")
        self.assertEqual(records["bad"]["exit_code"], 3)
        self.assertEqual(records["good"]["status"], "passed")
        for name, index in (("bad", 1), ("good", 2)):
            log = Path(active["validation_dir"]) / str(index) / "check-0.log"
            self.assertTrue(log.is_file(), name)
            self.assertEqual(records[name]["log"], str(log))

    def test_a_default_failure_still_holds_back_the_extended_checks(self):
        checks = [self.check("quick", tier="default", code=1), self.check("later")]
        records, _ = self.validate(self.runner(checks), expect=False)
        self.assertEqual(records["later"]["skipped_after"], "quick")
        self.assertEqual([t["name"] for t in self.traced()], ["quick"])

    def test_a_timeout_stops_only_its_own_check_and_waiting_does_not_count(self):
        checks = [self.check("quick", tier="default"), self.check("stuck", 30, timeout_seconds=1),
                  self.check("waits", 0.1, timeout_seconds=1), self.check("other", 1.5)]
        runner = self.runner(checks, cpus="1,2")
        write_json(runner.root / "check-cache.json", {
            name: {"status": "passed", "exit_code": 0, "key": "stale", "started_at": "2026-10-22T00:00:00+00:00",
                   "finished_at": f"2026-10-22T00:00:{seconds:02d}+00:00"}
            for name, seconds in (("stuck", 30), ("other", 20), ("waits", 1))})
        records, _ = self.validate(runner, expect=False)
        self.assertEqual(records["stuck"]["exit_code"], 124)
        self.assertIn("time limit", records["stuck"]["note"])
        # "other" outlived the timeout, and "waits" waited for a CPU longer than its own limit.
        self.assertEqual(records["other"]["status"], "passed")
        self.assertEqual(records["waits"]["status"], "passed")
        self.assertEqual({t["name"] for t in self.traced()}, {"quick", "waits", "other"})

    def test_keys_do_not_depend_on_the_cpu_or_the_length_of_the_list(self):
        checks = [self.check("quick", tier="default"), self.check("a")]
        first, second = self.runner(checks, cpus="1,2,3"), self.runner(checks, cpus="5")
        for spec_a, spec_b in zip(first.config["checks"], second.config["checks"]):
            self.assertEqual(spec_a["command"], spec_b["command"])
            self.assertIn("{check_cpu}", spec_a["command"])
            self.assertEqual(first.check_keys(spec_a), second.check_keys(spec_b))
        records, _ = self.validate(first)
        # The batch's last issue keys its extended checks on the worktree (W-507); keys come back after the pool.
        self.assertEqual(records["a"]["key"], first.check_keys(first.config["checks"][1], base=self.base,
                                                               worktree=fingerprint(self.repo))[0])
        # A second validation of the same bytes reuses the evidence.
        again, _ = self.validate(first)
        self.assertTrue(again["a"]["reused"])


class StopTests(Harness):
    def family(self):
        script = self.root / "family.py"
        script.write_text(FAMILY)
        self.pids = self.root / "pids.txt"
        return {"name": "family", "kind": "code", "tier": "extended", "inputs": ["lib.txt"], "cwd": ".",
                "command": ["${python}", str(script), str(self.pids)]}

    def wait_for_pids(self, count):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.pids.exists() and len(self.pids.read_text().split()) == count:
                return [int(pid) for pid in self.pids.read_text().split()]
            time.sleep(0.05)
        self.fail("the fake checks did not start")

    def jobs(self, runner, count):
        jobs = []
        for index in range(count):
            directory = self.root / "pool" / str(index)
            directory.mkdir(parents=True)
            jobs.append({"directory": directory, "check": {k: self.family()[k] for k in ("cwd",)}
                         | {"command": runner.config["checks"][0]["command"]},
                         "environment": runner.config["check_environment"]})
        return jobs

    def test_sigterm_during_the_pool_leaves_no_check_process(self):
        # The handler is the supervisor's: stop the children and unwind.
        runner = self.runner([self.family()], cpus="1,2")
        jobs = self.jobs(runner, 2)

        def interrupted(signum, frame):
            runner.stop_child()
            raise KeyboardInterrupt(f"Signal {signum}")
        previous = signal.signal(signal.SIGTERM, interrupted)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        seen = {}

        def sender():
            seen["pids"] = self.wait_for_pids(4)
            seen["state"] = list(runner.state.get("child_pids") or [])
            os.kill(os.getpid(), signal.SIGTERM)
        thread = threading.Thread(target=sender)
        thread.start()
        with self.assertRaises(KeyboardInterrupt):
            runner.run_pending(jobs, ["1", "2"], lambda job: None, poll_seconds=0.05)
        thread.join()
        self.assertEqual(len(seen["state"]), 2)
        for pid in seen["pids"]:
            self.assertTrue(dead_or_zombie(pid), pid)
        self.assertEqual(runner.check_children, {})
        self.assertEqual(runner.state["child_pids"], [])
        self.assertIsNone(live_child(runner.state))

    def test_an_error_in_the_pool_stops_the_others(self):
        runner = self.runner([self.family()], cpus="1,2")
        jobs = self.jobs(runner, 1)
        bad = self.root / "pool" / "bad"
        bad.mkdir()
        jobs.append({"directory": bad, "check": {"cwd": ".", "command": ["/nonexistent/program"]},
                     "environment": runner.config["check_environment"]})
        with self.assertRaises(OSError):
            runner.run_pending(jobs, ["1", "2"], lambda job: None, poll_seconds=0.05)
        self.assertEqual(runner.check_children, {})

    def test_stop_process_groups_ends_every_group_within_one_deadline(self):
        script = self.root / "family.py"
        script.write_text(FAMILY)
        self.pids = self.root / "pids.txt"
        processes = [subprocess.Popen([sys.executable, str(script), str(self.pids)], start_new_session=True)
                     for _ in range(2)]
        self.addCleanup(lambda: [p.kill() for p in processes])
        pids = self.wait_for_pids(4)
        started = time.monotonic()
        stop_process_groups(processes, grace_seconds=1)
        self.assertLess(time.monotonic() - started, 5)  # one deadline, not one per group
        for pid in pids:
            self.assertTrue(dead_or_zombie(pid), pid)


class RecoveryTests(Harness):
    """Each reader of the recorded children refuses while one lives, from either field."""

    def live(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process.pid

    def test_live_child_reads_both_fields(self):
        pid = self.live()
        self.assertEqual(live_child({"child_pid": pid}), pid)
        self.assertEqual(live_child({"child_pids": [pid]}), pid)
        self.assertEqual(live_child({"child_pid": None, "child_pids": [None, pid]}), pid)
        self.assertIsNone(live_child({"child_pid": None, "child_pids": []}))
        self.assertIsNone(live_child({}))

    def readers(self, runner):
        """(what, a callable that must refuse) for each reader that takes a runner."""
        return [("recovery", lambda: recovery._preflight(runner, "reason", "owner")),
                ("execute", lambda: runner.execute()),
                ("supervisor", lambda: Supervisor(runner, launch_id="L").run())]

    def test_each_reader_refuses_a_live_pid_in_either_field(self):
        pid = self.live()
        for state in ({"child_pids": [pid]}, {"child_pid": pid}):
            runner = self.runner([self.check("quick", tier="default")])
            runner.verify_config()
            runner.save(**state)
            for what, reader in self.readers(runner):
                with self.subTest(reader=what, state=list(state)):
                    with self.assertRaisesRegex((RecoveryError, RuntimeError, SupervisorRefused),
                                                f"PID {pid} may still be alive"):
                        reader()

    def test_repin_refuses_a_live_pid_in_child_pids(self):
        pid = self.live()
        runner = self.runner([self.check("quick", tier="default")])
        write_json(runner.root / RESOLVED_NAME, {"config_sha256": "pinned"})
        runner.save(child_pids=[pid], phase="paused", config_sha256="pinned")
        with self.assertRaisesRegex(RecoveryError, f"PID {pid} may still be alive"):
            recovery.recover_repin_config(runner.config, self.linear, reason="r", authorized_by="o")

    def test_launch_refuses_a_live_pid_in_child_pids(self):
        pid = self.live()
        runner = self.runner([self.check("quick", tier="default")])
        runner.verify_config()
        runner.save(child_pids=[pid])
        with self.assertRaisesRegex(launcher.LaunchError, f"PID {pid} may still be alive"):
            launcher.launch(runner.config, self.linear, backend="foreground", runner=runner)

    def test_launch_preflight_refuses_what_the_host_cannot_give(self):
        runner = self.runner([self.check("quick", tier="default")])
        runner.verify_config()
        with patch.object(launcher, "check_cpu_problem", return_value="check_cpus: CPU 9 is not online"):
            with self.assertRaisesRegex(launcher.LaunchError, "CPU 9 is not online"):
                launcher.preflight(runner.config, runner, launch_id="L-test")

    def test_reconcile_clears_both_fields(self):
        source = (Path(recovery.__file__)).read_text()
        self.assertIn("child_pid=None, child_pids=[]", source)


class DirectoryTests(Harness):
    PROJECT = {"check_directories": ["TMPDIR", "MPLCONFIGDIR"],
               "check_environment": {"TMPDIR": "${scratch}/tmp", "MPLCONFIGDIR": "${scratch}/mpl", "OTHER": "same"}}

    def script_for(self, name, code=0):
        self.scripts = getattr(self, "scripts", 0) + 1
        script = self.root / f"script{self.scripts}.py"
        script.write_text("import json, os, sys\n"
                          f"open({str(self.trace)!r}, 'a').write(json.dumps(dict(name={name!r}, tmp=os.environ['TMPDIR'], "
                          "mpl=os.environ['MPLCONFIGDIR'], other=os.environ['OTHER'], "
                          "exists=os.path.isdir(os.environ['TMPDIR']))) + '\\n')\n"
                          f"open(os.path.join(os.environ['TMPDIR'], 'left.txt'), 'w').write('x')\nsys.exit({code})\n")
        return script

    def spec(self, name, code=0, tier="extended"):
        return {"name": name, "kind": "code", "tier": tier, "inputs": ["lib.txt"], "cwd": ".",
                "command": ["${python}", str(self.script_for(name, code))]}

    def test_each_check_has_its_own_and_a_passed_checks_is_removed(self):
        runner = self.runner([self.spec("quick", tier="default"), self.spec("good"), self.spec("bad", code=1),
                              self.spec("a/../b")], project=self.PROJECT)
        records, active = self.validate(runner, expect=False)
        trace = {t["name"]: t for t in self.traced()}
        base, validation = self.root / "scratch" / "tmp", Path(active["validation_dir"]).name
        self.assertEqual(len({t["tmp"] for t in trace.values()}), 4)
        for index, name in enumerate(("quick", "good", "bad", "a/../b")):
            self.assertEqual(trace[name]["tmp"], str(base / validation / str(index)), name)
            self.assertEqual(trace[name]["mpl"], str(self.root / "scratch" / "mpl" / validation / str(index)))
            self.assertTrue(trace[name]["exists"])
            self.assertEqual(records[name]["environment_overrides"]["TMPDIR"], trace[name]["tmp"])
            self.assertEqual(trace[name]["other"], "same")
        self.assertFalse(Path(trace["good"]["tmp"]).exists())
        self.assertFalse(Path(trace["a/../b"]["tmp"]).exists())
        self.assertTrue((Path(trace["bad"]["tmp"]) / "left.txt").is_file())
        # A retry is another validation: it does not reuse the failed run's directory.
        self.trace.unlink()
        self.validate(runner, expect=False)
        retry = {t["name"]: t for t in self.traced()}
        self.assertNotEqual(retry["bad"]["tmp"], trace["bad"]["tmp"])
        self.assertTrue((Path(trace["bad"]["tmp"]) / "left.txt").is_file())

    def test_a_model_session_the_baseline_and_delivery_get_the_value_unchanged(self):
        runner = self.runner([self.spec("quick", tier="default")], project=self.PROJECT)
        self.assertEqual(runner.config["check_environment"]["TMPDIR"], str(self.root / "scratch" / "tmp"))
        directory = self.root / "plain"; directory.mkdir()
        (self.root / "scratch" / "tmp").mkdir(parents=True)
        self.assertTrue(runner.validate(directory, [{k: runner.config["checks"][0][k] for k in CHECK_RUN_KEYS
                                                     if k in runner.config["checks"][0]}],
                                        runner.config["check_environment"]))
        self.assertEqual(self.traced()[0]["tmp"], str(self.root / "scratch" / "tmp"))


class WhoRunsWhereTests(Harness):
    def cpus_check(self, name, tier="default", variable="${all_check_cpus}"):
        code = (f"import sys; open({str(self.trace)!r}, 'a').write(__import__('json').dumps("
                f"dict(name={name!r}, cpu=sys.argv[1], all=sys.argv[2])) + '\\n')")
        return {"name": name, "kind": "code", "tier": tier, "inputs": ["lib.txt"], "cwd": ".",
                "command": ["${python}", "-c", code, "${check_cpu}", variable]}

    def test_default_baseline_and_delivery_get_the_first_cpu_and_the_whole_list(self):
        runner = self.runner([self.cpus_check("quick")], cpus="4,5,6")
        self.validate(runner)
        self.assertEqual(self.traced()[-1], {"name": "quick", "cpu": "4", "all": "4,5,6"})
        self.trace.unlink()
        baseline = self.root / "baseline"; baseline.mkdir()
        launcher._baseline_checks(runner, baseline, reuse=False)
        self.assertEqual(self.traced()[-1], {"name": "quick", "cpu": "4", "all": "4,5,6"})
        self.trace.unlink()
        directory = self.root / "delivery"; directory.mkdir()
        spec = runner.config["checks"][0]
        runner.validate(directory, [{"cwd": spec["cwd"], "command": spec["command"]}], runner.config["check_environment"])
        self.assertEqual(self.traced()[-1], {"name": "quick", "cpu": "4", "all": "4,5,6"})

    def test_a_base_parity_check_gets_the_first_cpu(self):
        out = "out.txt"
        spec = {"name": "parity", "kind": "code", "tier": "extended", "inputs": ["lib.txt"], "cwd": ".",
                "base_parity": {"outputs": [out]},
                "command": ["${python}", "-c", f"import sys; open({out!r}, 'w').write(sys.argv[1])", "${check_cpu}"]}
        (self.repo / ".gitignore").write_text(out + "\n")
        git(self.repo, "add", "--all"); git(self.repo, "commit", "-qm", "ignore")
        self.base = git(self.repo, "rev-parse", "HEAD~1")
        records, _ = self.validate(self.runner([self.check("quick", tier="default"), spec], cpus="4,5"))
        self.assertEqual(records["parity"]["status"], "passed")
        self.assertIn("4", records["parity"]["command"])
        self.assertEqual((self.repo / out).read_text(), "4")

    def test_without_a_list_both_mean_the_supervisors_cpu_list(self):
        self.validate(self.runner([self.cpus_check("quick")], cpus=None, cpu_list="7"))
        self.assertEqual(self.traced()[-1], {"name": "quick", "cpu": "7", "all": "7"})

    def test_the_workers_packet_shows_the_batchs_cpu_list(self):
        runner = self.runner([self.cpus_check("quick")], cpus="4,5", cpu_list="9")
        active = {"issue": {"id": "DEV-1"}, "issue_id": "DEV-1", "starting_commit": self.base}
        packet = build_packet(runner.config, active, {}, [])
        command = packet["checks"][0]["command"]
        self.assertEqual(command[-2:], ["9", "9"])
        self.assertNotIn("check_cpu", json.dumps(packet["checks"]))
        self.assertEqual(runner.config["checks"][0]["command"][-2:], ["{check_cpu}", "{all_check_cpus}"])


if __name__ == "__main__":
    unittest.main()
