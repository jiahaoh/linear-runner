"""The check CPU list and the variables that name it (W-746)."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from linear_runner import cli
from linear_runner.config import (ConfigError, check_cpu_problem, checks_for_worker, config_fingerprint, cpu_numbers,
                                  load_config)
from tests.fixtures import make_home


def check(name="one", tier="default", command=("${python}", "-c", "pass", "${check_cpu}"), **fields):
    return dict({"name": name, "kind": "code", "tier": tier, "inputs": ["*"], "cwd": ".", "command": list(command)}, **fields)


class CheckCpuConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"

    def load(self, cpus="2,4", *, checks=None, launcher="0", batch=None, **project):
        site = {"variables": {"check_cpus": cpus}} if cpus else {}
        if launcher is not None:
            site["launcher"] = {"cpu_list": launcher}
        layers = {"site": site, "project": dict({"checks": checks or [check()]}, **project)}
        if batch:
            layers["batch"] = batch
        home, path = make_home(self.root, self.repo, **layers)
        return load_config(path, home)

    def refused(self, pattern, *args, **kwargs):
        with self.assertRaisesRegex(ConfigError, pattern):
            self.load(*args, **kwargs)

    def test_the_list_resolves_and_commands_keep_literals(self):
        loaded = self.load("2,4-6")
        self.assertEqual(loaded["check_cpus"], ["2", "4", "5", "6"])
        self.assertEqual(loaded["checks"][0]["command"][-1], "{check_cpu}")

    def test_a_batch_overrides_the_list(self):
        loaded = self.load("2,4", batch={"variables": {"check_cpus": "6,7"}})
        self.assertEqual(loaded["check_cpus"], ["6", "7"])
        self.assertEqual(loaded["_sources"]["check_cpus"], "batch fixture")

    def test_a_bad_list_is_refused(self):
        for bad in ("all", "2,", "2,,4", "4-2", "2,2", "2,1-3"):
            with self.subTest(bad=bad):
                self.refused("variables.check_cpus", bad)

    def test_an_overlap_with_the_supervisors_cpus_is_refused(self):
        self.refused("CPU 2 is also in site.launcher.cpu_list", "2,4", launcher="0-2")

    def test_a_cpu_variable_with_no_list_at_all_is_refused(self):
        self.refused("neither variables.check_cpus nor site.launcher.cpu_list", None, launcher=None)
        self.assertEqual(self.load(None, launcher="1")["checks"][0]["command"][-1], "{check_cpu}")

    def test_the_variables_are_accepted_only_in_a_command(self):
        def environment(name, value):
            return {"check_environment": {name: value}}
        self.refused("undefined variable", **environment("X", "${check_cpu}"))
        self.refused("undefined variable", **environment("X", "${all_check_cpus}"))
        self.refused("undefined variable", checks=[check(environment={"X": "${check_cpu}"})])
        self.refused("undefined variable", checks=[check(cwd="${check_cpu}")])
        self.refused("undefined variable", checks=[check(command=("${python}", "-c", "pass"))],
                     check_environment={"X": "${all_check_cpus}"})
        home, path = make_home(self.root, self.repo, site={"launcher": {"cpu_list": "0", "environment": {"X": "${check_cpu}"}}})
        with self.assertRaisesRegex(ConfigError, "undefined variable"):
            load_config(path, home)

    def test_a_cpu_variable_cannot_be_the_executable(self):
        self.refused("cannot be the executable", checks=[check(command=("${check_cpu}", "-c", "pass"))])
        self.refused("cannot be the executable", checks=[check(command=("${all_check_cpus}",))])

    def test_a_list_needs_every_pooled_check_to_name_its_cpu(self):
        unpinned = check(name="unpinned", tier="extended", command=("${python}", "-c", "pass"))
        self.refused("unpinned would run at the same time", checks=[unpinned])
        self.assertEqual(self.load(None, checks=[unpinned])["checks"][0]["name"], "unpinned")  # no list, no rule
        self.load(checks=[dict(unpinned, base_parity={"outputs": ["out.txt"]})])  # base parity runs in sequence
        self.load(checks=[check(name="default", command=("${python}", "-c", "pass"))])  # so do default checks

    def test_a_check_cannot_set_or_unset_a_per_check_directory(self):
        project = {"check_directories": ["TMPDIR"], "check_environment": {"TMPDIR": "/absolute/tmp"}}
        self.refused("is in check_directories", checks=[check(environment={"TMPDIR": "/other"})], **project)
        self.refused("is in check_directories", checks=[check(unset_environment=["TMPDIR"])], **project)

    def test_the_whole_list_is_for_a_default_check(self):
        self.load(checks=[check(command=("${python}", "${all_check_cpus}"))])
        self.refused("is for a default check", checks=[check(tier="extended", command=("${python}", "${all_check_cpus}"))])

    def test_a_site_or_batch_cannot_define_the_built_ins(self):
        home, path = make_home(self.root, self.repo, site={"variables": {"check_cpu": "1"}})
        with self.assertRaisesRegex(ConfigError, "already defined"):
            load_config(path, home)
        home, path = make_home(self.root, self.repo, site={"variables": {"x": "1"}},
                               batch={"variables": {"all_check_cpus": "1"}})
        with self.assertRaisesRegex(ConfigError, "built-in variable"):
            load_config(path, home)

    def test_delivery_checks_accept_the_variables(self):
        loaded = self.load(delivery_checks=[{"cwd": ".", "command": ["${python}", "${check_cpu}", "${all_check_cpus}"]}])
        self.assertEqual(loaded["delivery_checks"][0]["command"][1:], ["{check_cpu}", "{all_check_cpus}"])

    def test_check_directories_name_absolute_paths_in_the_environment(self):
        loaded = self.load(check_directories=["TMPDIR"], check_environment={"TMPDIR": "/absolute/tmp"})
        self.assertEqual(loaded["check_directories"], ["TMPDIR"])
        self.refused("must be an absolute path", check_directories=["TMPDIR"], check_environment={"TMPDIR": "relative"})
        self.refused("must be an absolute path", check_directories=["ABSENT"])
        self.refused("check_directories", check_directories=["no/slash"], check_environment={"no": "/x"})

    def test_neither_key_keeps_the_fingerprint_of_a_project_that_does_not_use_them(self):
        plain = [check(command=("${python}", "-c", "pass"))]
        loaded = self.load(None, checks=plain)
        self.assertNotIn("check_cpus", loaded)
        self.assertNotIn("check_directories", loaded)
        self.assertEqual(config_fingerprint(loaded), config_fingerprint(self.load(None, checks=plain)))
        self.assertNotEqual(config_fingerprint(loaded), config_fingerprint(self.load("2,4", checks=plain)))

    def test_the_worker_sees_the_batchs_own_cpu(self):
        loaded = self.load("2,4", launcher="9", checks=[check(command=("${python}", "${check_cpu}", "${all_check_cpus}"))])
        self.assertEqual(checks_for_worker(loaded)[0]["command"][1:], ["9", "9"])
        self.assertEqual(loaded["checks"][0]["command"][1:], ["{check_cpu}", "{all_check_cpus}"])
        loaded = self.load("2,4", launcher=None, checks=[check(command=("${python}", "${check_cpu}"))])
        self.assertEqual(checks_for_worker(loaded)[0]["command"][1], "2,4")

    def test_validate_config_lists_the_cpus_and_refuses_what_the_host_cannot_give(self):
        loaded = self.load("2,4", check_directories=["TMPDIR"], check_environment={"TMPDIR": "/absolute/tmp"})
        report = cli.summarize(loaded)
        self.assertEqual(report["check_cpus"], ["2", "4"])
        self.assertEqual(report["check_directories"], ["TMPDIR"])
        self.assertIsNone(cli.summarize(self.load(None))["check_cpus"])
        home, path = make_home(self.root, self.repo, site={"variables": {"check_cpus": "2"}, "launcher": {"cpu_list": "0"}},
                               project={"checks": [check()]})
        with patch.object(cli, "check_cpu_problem", return_value="check_cpus: CPU 2 is not online"), \
                contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
            cli.main(["validate-config", "--home", str(home), "--batch", str(path)])
        self.assertIn("CPU 2 is not online", errors.getvalue())


class CheckCpuHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.sys = Path(self.tmp.name)
        (self.sys / "online").write_text("0-3\n")
        for cpu, siblings in {0: "0,2", 1: "1,3", 2: "0,2", 3: "1,3"}.items():
            topology = self.sys / f"cpu{cpu}" / "topology"
            topology.mkdir(parents=True)
            (topology / "thread_siblings_list").write_text(siblings + "\n")

    def test_one_cpu_per_core_passes(self):
        self.assertIsNone(check_cpu_problem(["0", "1"], self.sys))

    def test_two_cpus_of_one_core_are_refused(self):
        self.assertRegex(check_cpu_problem(["0", "2"], self.sys), "CPUs 0 and 2 are hyper-threads of one physical core")

    def test_an_offline_cpu_is_refused(self):
        self.assertRegex(check_cpu_problem(["1", "7"], self.sys), "CPU 7 is not online")

    def test_files_that_cannot_be_read_check_nothing(self):
        self.assertIsNone(check_cpu_problem(["0", "7"], self.sys / "absent"))

    def test_cpu_numbers(self):
        self.assertEqual(cpu_numbers("2,4-6", "x"), ["2", "4", "5", "6"])


if __name__ == "__main__":
    unittest.main()
