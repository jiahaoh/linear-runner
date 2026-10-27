"""Inert changes (W-605): the controller's classification, the selection of extended checks it
drives, and the inert record (real Git and checks, fake Linear and model sessions)."""
import copy
import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest

from linear_runner.config import load_config, pin_resolution
from linear_runner.engine import inert
from linear_runner.engine.delivery import (INERT_NOTE, SKIPPED_NOTE, DeliveryError, _intact, check_deferred,
                                           check_inert, check_passed, check_skipped, evidence_intact, verify_delivery)
from linear_runner.engine.runner import Runner, expected_seconds, git, write_json
from linear_runner.linear import messages
from linear_runner.reporting import measure, trajectory
from linear_runner.reporting.records import INERT_OUTCOME, check_status, outcome_text
from tests.fixtures import TEST_REGISTRY, FakeLinear, make_home, make_repo, write

TEST_MODULE = '''"""Metrics of the spot finder.

Cites section 2.7.
"""
import json

SUFFIX = ".json"


class Metrics:
    """Precision and recall."""

    def score(self, a, b):
        """The F1 score."""
        # the harmonic mean
        return 2 * a * b / (a + b)


async def load(path):
    """Read a metrics file."""
    return json.loads(path)
'''


def edited(*replacements):
    text = TEST_MODULE
    for old, new in replacements:
        assert old in text, old
        text = text.replace(old, new)
    return text


# The module, class, method and async-function docstrings; the module docstring grows by a line.
DOCSTRINGS = edited(("Cites section 2.7.\n", "Cites section 2.7, Spot finding.\nSee the metrics contract.\n"),
                    ("Precision and recall.", "Precision, recall and F1."), ("The F1 score.", "The F1 score of two rates."),
                    ("Read a metrics file.", "Read one metrics file."))
COMMENTS = edited(("import json\n", "import json  # standard library\n"),
                  ("        # the harmonic mean\n", "        # the harmonic mean of a and b\n        # noqa: E501\n"))
STATEMENT = edited(("return 2 * a * b / (a + b)", "return 2 * a * b / (a + b + 1e-9)"))
LITERAL = edited(('SUFFIX = ".json"', 'SUFFIX = ".csv"'))  # a string literal that is no docstring
README = "tests/data/metrics/README.md"
FILES = {"tests/test_metrics.py": TEST_MODULE, README: "# Metrics data\n\nThe tests read three files here.\n",
         "docs/other.md": "# Other\n", "docs/other.rst": "Other\n=====\n", "docs/read-by-tests.md": "# Read by tests\n",
         "docs/contracts.md": "# Contracts\n", "lib/code.py": "def value():\n    return 1\n"}


class Harness(unittest.TestCase):
    """FILES committed on a baseline; a batch of DEV-1 and DEV-2, so DEV-1 is not the last issue;
    every check appends its name to a marker outside the repository when it runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        make_repo(self.repo)
        self.commit(FILES, "fixture files")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.home, self.batch = make_home(self.root, self.repo)
        self.linear = FakeLinear()
        self.config, _ = pin_resolution(load_config(self.batch, self.home), self.linear)
        self.runner = Runner(self.config, self.linear)
        self.config["issues"] = ["DEV-1", "DEV-2"]
        self.linear.add_issue("DEV-2")
        self.marker = self.root / "runs.txt"
        self.validations = 0

    def commit(self, files, message):
        for name, text in files.items():
            write(self.repo / name, text)
        git(self.repo, "add", "--all"); git(self.repo, "commit", "-qm", message)
        return git(self.repo, "rev-parse", "HEAD")

    def check(self, name, inputs, tier="extended", kind="code", **options):
        code = f"open({str(self.marker)!r}, 'a').write({name!r} + '\\n')"
        return dict({"name": name, "kind": kind, "tier": tier, "inputs": list(inputs), "cwd": ".",
                     "command": [sys.executable, "-c", code]}, **options)

    def quick(self):
        """A default check on files the tests here leave alone, so that a validation has one."""
        return self.check("quick", ["lib/*"], tier="default")

    def runs(self):
        """The checks executed so far, in order: each command writes its own line."""
        return self.marker.read_text().splitlines() if self.marker.exists() else []

    def active(self, issue="DEV-1", starting_commit=None):
        self.validations += 1
        run = self.root / "runs" / issue / f"20261026T2335{self.validations:02d}Z-{self.validations:08x}"
        run.mkdir(parents=True)
        return {"run_dir": str(run), "issue_id": issue, "starting_commit": starting_commit or self.base}

    def validate(self, issue="DEV-1", starting_commit=None):
        """Run the checks of one validation; return its records by name."""
        self.last = self.active(issue, starting_commit)
        self.assertTrue(self.runner.run_checks(self.last))
        return {r["name"]: r for r in json.loads((Path(self.last["validation_dir"]) / "checks.json").read_text())}

    def assert_inert(self, record, files, base=None, scope="issue"):
        self.assertEqual(record, {"name": record["name"], "status": "skipped", "exit_code": None, "key": None,
                                  "reused": False, "model": None, "note": INERT_NOTE,
                                  "inert": {"base": base or self.base, "scope": scope,
                                            "files": [{"path": path, "rule": rule} for path, rule in files]}})
        self.assertTrue(check_inert(record) and check_passed(record))


class ClassifyTests(Harness):
    def test_each_rule_and_each_file_that_is_never_inert(self):
        write(self.repo / "tests/test_metrics.py", DOCSTRINGS)
        write(self.repo / "docs/other.md", "# Other, reworded\n")
        write(self.repo / "docs/read-by-tests.md", "# Read by tests, changed\n")
        write(self.repo / "lib/code.py", "def value():\n    return 2\n")
        write(self.repo / "tests/test_new.py", '"""Only a docstring."""\n')
        (self.repo / "docs/contracts.md").unlink()
        (self.repo / "docs/contracts.md").symlink_to("other.md")
        index = (self.repo / ".git" / "index").read_bytes()
        paths = ["tests/test_metrics.py", "docs/other.md", "docs/read-by-tests.md", "lib/code.py", "tests/test_new.py",
                 "docs/contracts.md", "docs/missing.md"]
        self.assertEqual(inert.classify(self.repo, self.base, paths, ["docs/read-by-tests.md"]),
                         {"tests/test_metrics.py": "python-ast", "docs/other.md": "documentation",
                          "docs/read-by-tests.md": None, "lib/code.py": None, "tests/test_new.py": None,
                          "docs/contracts.md": None, "docs/missing.md": None})
        # Without the project's list no document is inert; an empty list says no test reads one.
        self.assertEqual(inert.classify(self.repo, self.base, ["docs/other.md"]), {"docs/other.md": None})
        self.assertEqual(inert.classify(self.repo, self.base, ["docs/other.md"], []), {"docs/other.md": "documentation"})
        # The comparison reads blobs and files only: the index is untouched.
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index)
        # A base Git cannot read proves nothing inert.
        self.assertEqual(inert.classify(self.repo, "0" * 40, ["tests/test_metrics.py"]), {"tests/test_metrics.py": None})

    def test_a_file_behind_a_directory_that_a_symlink_replaced_is_never_inert(self):
        # Git lists the base's file as deleted; the path now reaches a file of the same text elsewhere.
        (self.repo / "tests/data/metrics").rename(self.repo / "moved")
        (self.repo / "tests/data/metrics").symlink_to("../../moved")
        self.assertIn(README, git(self.repo, "diff", "--name-only", self.base).splitlines())
        self.assertEqual(inert.classify(self.repo, self.base, [README], []), {README: None})
        self.assertEqual(inert.classify(self.repo, self.base, ["moved/README.md"], []), {"moved/README.md": None})

    def test_a_coding_declaration_applies_and_other_suffixes_are_never_inert(self):
        latin = b'# -*- coding: latin-1 -*-\n"""Caf\xe9 metrics."""\nx = 1\n'
        (self.repo / "tests" / "latin.py").write_bytes(latin)
        write(self.repo / "tests/config.toml", "# a comment\n")
        base = self.commit({}, "latin-1 module and a TOML file")
        (self.repo / "tests" / "latin.py").write_bytes(latin.replace(b"Caf\xe9 metrics", b"Caf\xe9 metrics, revised"))
        write(self.repo / "tests/config.toml", "# another comment\n")
        self.assertEqual(inert.classify(self.repo, base, ["tests/latin.py", "tests/config.toml"]),
                         {"tests/latin.py": "python-ast", "tests/config.toml": None})

    def test_the_classification_takes_no_worker_or_reviewer_input(self):
        # The controller decides alone: the repository, the base commit, the paths and the project's patterns.
        self.assertEqual(list(inspect.signature(inert.classify).parameters),
                         ["repo", "base", "paths", "documents_read_by_tests"])


class SelectionTests(Harness):
    def test_a_docstring_only_change_is_not_run_and_recorded_as_inert(self):
        self.assertEqual(DOCSTRINGS.count("\n"), TEST_MODULE.count("\n") + 1)  # one docstring grew by a line
        self.config["checks"] = [self.quick(), self.check("slow", ["tests/*.py"])]
        write(self.repo / "tests/test_metrics.py", DOCSTRINGS)
        records = self.validate()
        self.assertEqual(self.runs(), ["quick"])
        self.assert_inert(records["slow"], [("tests/test_metrics.py", "python-ast")])
        self.assertFalse(check_skipped(records["slow"]) or check_deferred(records["slow"]))
        self.assertFalse((Path(self.last["validation_dir"]) / "1").exists())  # no evidence directory either

    def test_comments_are_inert_and_a_statement_or_a_literal_is_not(self):
        self.config["checks"] = [self.quick(), self.check("slow", ["tests/*.py"])]
        write(self.repo / "tests/test_metrics.py", COMMENTS)
        self.assert_inert(self.validate()["slow"], [("tests/test_metrics.py", "python-ast")])
        for text in (STATEMENT, LITERAL):
            write(self.repo / "tests/test_metrics.py", text)
            self.assertEqual(self.validate()["slow"]["status"], "passed")
        self.assertEqual(self.runs(), ["quick", "slow", "slow"])

    def assert_runs(self, paths):
        """The check `slow` ran on the change, and none of ``paths`` is inert."""
        self.config["checks"] = [self.quick(), self.check("slow", ["tests/*"])]
        records = self.validate()
        self.assertEqual((records["slow"]["status"], self.runs()), ("passed", ["quick", "slow"]))
        self.assertNotIn("inert", records["slow"])
        self.assertEqual(set(inert.classify(self.repo, self.base, paths).values()), {None})

    def test_an_added_file_is_never_inert(self):
        write(self.repo / "tests/test_new.py", '"""Only a docstring."""\n')
        self.assert_runs(["tests/test_new.py"])

    def test_a_deleted_file_is_never_inert(self):
        (self.repo / "tests/test_metrics.py").unlink()
        self.assert_runs(["tests/test_metrics.py"])

    def test_a_renamed_file_is_never_inert(self):
        git(self.repo, "mv", "tests/test_metrics.py", "tests/test_scores.py")
        self.assert_runs(["tests/test_metrics.py", "tests/test_scores.py"])

    def test_a_changed_executable_bit_is_never_inert(self):
        write(self.repo / "tests/test_metrics.py", DOCSTRINGS)
        (self.repo / "tests/test_metrics.py").chmod(0o755)
        self.assert_runs(["tests/test_metrics.py"])
        (self.repo / "tests/test_metrics.py").chmod(0o644)  # the same text without the mode change is inert
        self.assertEqual(inert.classify(self.repo, self.base, ["tests/test_metrics.py"]),
                         {"tests/test_metrics.py": "python-ast"})

    def test_a_file_that_does_not_parse_on_one_side_is_never_inert(self):
        write(self.repo / "tests/test_metrics.py", DOCSTRINGS + "\ndef broken(:\n")
        self.assert_runs(["tests/test_metrics.py"])
        # The other side: the base does not parse, the worktree does.
        base = self.commit({"tests/test_broken.py": "def broken(:\n"}, "a module that does not parse")
        write(self.repo / "tests/test_broken.py", '"""Fixed."""\n')
        self.assertEqual(inert.classify(self.repo, base, ["tests/test_broken.py"]), {"tests/test_broken.py": None})

    def test_documentation_outside_the_projects_list_is_inert(self):
        self.config["documents_read_by_tests"] = ["docs/read-by-tests.md"]
        self.config["checks"] = [self.quick(), self.check("slow", ["docs/*"])]
        write(self.repo / "docs/other.md", "# Other, reworded\n")
        write(self.repo / "docs/other.rst", "Other\n=====\n\nReworded.\n")
        self.assert_inert(self.validate()["slow"], [("docs/other.md", "documentation"), ("docs/other.rst", "documentation")])
        # A document the tests read runs the check.
        write(self.repo / "docs/read-by-tests.md", "# Read by tests, changed\n")
        self.assertEqual(self.validate()["slow"]["status"], "passed")
        # Without the key no document is inert.
        git(self.repo, "checkout", "--", "docs/read-by-tests.md")
        del self.config["documents_read_by_tests"]
        self.assertEqual(self.validate()["slow"]["status"], "passed")
        self.assertEqual(self.runs(), ["quick", "slow", "slow"])

    def test_a_mixed_change_runs_only_the_check_that_matches_code(self):
        self.config["documents_read_by_tests"] = ["docs/read-by-tests.md"]
        self.config["checks"] = [self.quick(), self.check("a", ["tests/*"]), self.check("b", ["tests/*", "lib/*"])]
        write(self.repo / "tests/test_metrics.py", DOCSTRINGS)
        write(self.repo / README, "# Metrics data\n\nThe tests read three files here, listed below.\n")
        write(self.repo / "lib/code.py", "def value():\n    return 2\n")
        records = self.validate()
        self.assert_inert(records["a"], [(README, "documentation"), ("tests/test_metrics.py", "python-ast")])
        self.assertEqual(records["b"]["status"], "passed")
        self.assertEqual(self.runs(), ["quick", "b"])
        # One check whose matched files are the inert ones and one code change runs.
        self.config["checks"] = [self.quick(), self.check("c", ["tests/test_metrics.py", "tests/data/*", "lib/code.py"])]
        self.assertEqual(self.validate()["c"]["status"], "passed")
        self.assertEqual(self.runs(), ["quick", "b", "c"])

    def test_an_opted_out_check_and_a_docs_check_run_on_a_docstring_only_change(self):
        self.config["checks"] = [self.quick(), self.check("doctest", ["tests/*.py"], inert_changes="run"),
                                 self.check("strict-docs", ["tests/*.py"], kind="docs"), self.check("slow", ["tests/*.py"])]
        write(self.repo / "tests/test_metrics.py", DOCSTRINGS)
        records = self.validate()
        self.assertEqual((records["doctest"]["status"], records["strict-docs"]["status"]), ("passed", "passed"))
        self.assertTrue(check_inert(records["slow"]))
        self.assertEqual(sorted(self.runs()), ["doctest", "quick", "strict-docs"])

    def test_a_worker_summary_does_not_make_a_code_change_inert(self):
        # The controller decides alone: the worker calls its code change "comments only".
        self.config["checks"] = [self.config["checks"][0], self.check("slow", ["lib/*"])]
        def session(prompt, directory, **kwargs):
            phase = Path(directory).name.split("-")[0]
            Path(directory).mkdir(parents=True)
            if phase == "implement":
                (self.repo / "result.txt").write_text("ready")
                (self.repo / "lib/code.py").write_text("def value():\n    return 2\n")
            write_json(Path(directory) / "session.json", {"session_id": phase, "requested_model": kwargs["model"],
                       "requested_reasoning_effort": kwargs["effort"], "exit_code": 0,
                       "execution_evidence": {"usage_events": [{"usage": {"input_tokens": 100, "output_tokens": 5}}]}})
            return ({"issue_id": "DEV-1", "status": "ready", "commit": git(self.repo, "rev-parse", "HEAD"),
                     "summary": "Comments only: no code changed, so no extended check needs to run.",
                     "acceptance": [{"criterion": "Produce validated output", "satisfied": True,
                                     "evidence": "result.txt and the output check"}], "limitations": []},
                    [], kwargs.get("resume") or phase)
        self.runner.run_session = session
        self.runner.execute(limit=1)
        self.assertEqual(self.linear.data["statusType"], "completed")
        validation = Path(self.runner.state["history"][0]["validation_dir"])
        records = {r["name"]: r for r in json.loads((validation / "checks.json").read_text())}
        self.assertEqual(records["slow"]["status"], "passed")
        self.assertNotIn("inert", records["slow"])
        self.assertEqual(self.runs(), ["slow"])

    def test_the_motivating_case_runs_only_the_documentation_check(self):
        # W-591 in hk-g-stages-20261026: a module-docstring citation in a test file and a README in a
        # test-data directory ran four extended checks for about 39 minutes; a documentation page
        # changed in the same commit selected the documentation check, which still runs.
        self.config["documents_read_by_tests"] = ["docs/read-by-tests.md"]
        self.config["checks"] = [self.quick(),
                                 self.check("learned-fast", ["tests/*", "lib/*"], last_issue="when_changed"),
                                 self.check("learned-slow", ["tests/*", "lib/*"], last_issue="when_changed"),
                                 self.check("slow-metrics", ["tests/test_metrics.py", "tests/data/metrics/*"],
                                            last_issue="when_changed"),
                                 self.check("strict-docs", ["docs/*"], kind="docs")]
        write(self.repo / "tests/test_metrics.py", edited(("Cites section 2.7.\n", "Cites section 2.7, Spot finding.\n")))
        write(self.repo / README, "# Metrics data\n\nThe tests read three files here; see the contract.\n")
        write(self.repo / "docs/contracts.md", "# Contracts\n\nSpot finding.\n")
        records = self.validate()
        for name in ("learned-fast", "learned-slow", "slow-metrics"):
            self.assert_inert(records[name], [(README, "documentation"), ("tests/test_metrics.py", "python-ast")])
        self.assertEqual(records["strict-docs"]["status"], "passed")
        self.assertEqual(self.runs(), ["quick", "strict-docs"])


class LastIssueTests(Harness):
    def test_an_always_check_runs_at_the_last_issue_on_a_docstring_only_change(self):
        self.config["checks"] = [self.check("slow", ["tests/*.py"])]
        write(self.repo / "tests/test_metrics.py", DOCSTRINGS)
        self.linear.data.update(status="Done", statusType="completed")  # DEV-2 is now the last issue
        records = self.validate("DEV-2")
        self.assertEqual((records["slow"]["status"], records["slow"]["reuse_scope"]), ("passed", "worktree"))
        self.assertNotIn("inert", records["slow"])
        self.assertEqual(self.runs(), ["slow"])

    def test_a_check_named_in_check_overrides_runs_at_its_issue_on_a_docstring_only_change(self):
        self.config["checks"] = [self.check("slow", ["tests/*.py"])]
        self.config["check_overrides"] = {"DEV-1": {"run": ["slow"]}}
        write(self.repo / "tests/test_metrics.py", DOCSTRINGS)
        records = self.validate("DEV-1")
        self.assertEqual((records["slow"]["status"], records["slow"]["reuse_scope"], records["slow"]["run_reason"]),
                         ("passed", "worktree", "named in the batch's check_overrides for DEV-1"))
        self.assertEqual(self.runs(), ["slow"])

    def test_a_when_changed_check_at_the_last_issue_compares_the_batch_with_its_base(self):
        self.config["issues"] = ["DEV-1", "DEV-2", "DEV-3"]
        self.linear.add_issue("DEV-3")
        self.config["documents_read_by_tests"] = ["docs/read-by-tests.md"]
        self.config["checks"] = [self.quick(), self.check("slow", ["tests/*"], last_issue="when_changed"),
                                 self.check("untouched", ["never/*"], last_issue="when_changed")]
        # Two finished issues: DEV-1 edited docstrings, DEV-2 a README that no test reads.
        second = self.commit({"tests/test_metrics.py": DOCSTRINGS}, "DEV-1")
        third = self.commit({README: "# Metrics data\n\nThe tests read three files here, listed below.\n"}, "DEV-2")
        self.runner.state["history"] = [{"issue_id": "DEV-1", "starting_commit": self.base},
                                        {"issue_id": "DEV-2", "starting_commit": second}]
        records = self.validate("DEV-3", starting_commit=third)
        self.assert_inert(records["slow"], [(README, "documentation"), ("tests/test_metrics.py", "python-ast")],
                          base=self.base, scope="batch")
        self.assertTrue(check_skipped(records["untouched"]))
        self.assertEqual(records["untouched"]["note"], SKIPPED_NOTE)
        self.assertEqual(self.runs(), ["quick"])
        # A code change at an earlier issue runs the check at the last issue.
        fourth = self.commit({"tests/test_metrics.py": STATEMENT}, "DEV-2, later: code")
        self.assertEqual(self.validate("DEV-3", starting_commit=fourth)["slow"]["status"], "passed")
        # The issue's own changes come first: DEV-3 reverts that code, which is not inert against
        # its starting commit, although the batch's change since the base is only docstrings again.
        write(self.repo / "tests/test_metrics.py", DOCSTRINGS)
        self.assertEqual(inert.classify(self.repo, self.base, ["tests/test_metrics.py"]),
                         {"tests/test_metrics.py": "python-ast"})
        self.assertEqual(self.validate("DEV-3", starting_commit=fourth)["slow"]["status"], "passed")
        self.assertEqual(self.runs(), ["quick", "slow", "slow"])


class RecordTests(Harness):
    RECORD = {"name": "slow", "status": "skipped", "exit_code": None, "key": None, "reused": False, "model": None,
              "inert": {"base": "0" * 40, "scope": "issue", "files": [{"path": "tests/test_metrics.py", "rule": "python-ast"}]},
              "note": INERT_NOTE}

    def test_an_inert_record_passes_without_a_log_and_is_no_evidence(self):
        record = self.RECORD
        self.assertTrue(check_inert(record) and check_passed(record))
        self.assertFalse(check_skipped(record) or check_deferred(record) or evidence_intact(record))
        _intact([record], "validation check")  # no log is looked for
        with self.assertRaisesRegex(DeliveryError, r"required check\(s\) \['slow'\] are not in the validated evidence"):
            verify_delivery({"required_checks": ["slow"], "manifest": "m.json", "revision_field": "revision"},
                            self.root, "0" * 40, [record])
        # A record that lacks any part of the inert block is neither inert nor passing.
        for broken in (dict(record, inert={}), dict(record, inert=dict(record["inert"], files=[])),
                       dict(record, inert=dict(record["inert"], scope="elsewhere")), dict(record, exit_code=1)):
            with self.subTest(broken=broken):
                self.assertFalse(check_inert(broken) or check_passed(broken))
                with self.assertRaises(DeliveryError):
                    _intact([broken], "validation check")

    def test_a_validation_of_inert_records_alone_is_not_a_validation(self):
        self.config["checks"] = [self.check("slow", ["tests/*.py"])]
        write(self.repo / "tests/test_metrics.py", DOCSTRINGS)
        with self.assertRaisesRegex(RuntimeError, "No applicable validation checks"):
            self.runner.run_checks(self.active())
        self.assertEqual(self.runs(), [])

    def test_the_low_risk_review_rule_treats_an_inert_record_as_passing(self):
        registry = copy.deepcopy(TEST_REGISTRY)
        registry["profiles"]["review_routing"] = {"light_review": {
            "profile": "Economy", "issue_profiles": ["Economy", "Standard"], "task_kinds": ["Implementation"],
            "max_changed_files": 8, "max_changed_lines": 300, "max_repairs": 0, "opt_out_labels": ["Full review"],
            "gate_labels": ["Human gate"]}}
        home, batch = make_home(self.root / "risk", self.repo, registry=registry,
                                batch={"context_controls": {"low_risk_review": True}})
        runner = Runner(pin_resolution(load_config(batch, home), self.linear)[0], self.linear)
        commit = self.commit({"tests/test_metrics.py": DOCSTRINGS}, "docstrings")
        active = dict(self.active(), issue=self.linear.issue("DEV-1"), repairs=0, commit=commit)
        validation = Path(active["run_dir"]) / "validation"
        passing = {"name": "quick", "status": "passed", "exit_code": 0}
        def risk(records):
            write_json(validation / "checks.json", records)
            return runner.review_risk(dict(active, validation_dir=str(validation)))
        validation.mkdir()
        self.assertEqual(risk([passing, self.RECORD])["eligible"], True)
        failing = risk([passing, dict(passing, name="slow", status="failed", exit_code=1)])
        self.assertIn("deterministic checks did not all pass", failing["failed"])

    def test_an_inert_record_is_not_cached_and_the_cached_record_keeps_its_order(self):
        # W-552 orders a tier by the latest passing record in the cache; an inert record leaves it there.
        def passing(seconds):
            return {"status": "passed", "exit_code": 0, "key": "stale", "started_at": "2026-10-22T00:00:00+00:00",
                    "finished_at": f"2026-10-22T00:00:{seconds:02d}+00:00"}
        cache_path = self.runner.root / "check-cache.json"
        write_json(cache_path, {"slow-a": passing(5), "slow-b": passing(50)})
        self.config["checks"] = [self.quick(), self.check("slow-b", ["lib/*"]), self.check("slow-a", ["tests/*.py"])]
        write(self.repo / "tests/test_metrics.py", DOCSTRINGS)
        self.assertTrue(check_inert(self.validate()["slow-a"]))
        cache = json.loads(cache_path.read_text())
        self.assertEqual((cache["slow-a"], expected_seconds(cache["slow-a"])), (passing(5), 5.0))
        # Both selected by code changes: slow-a, 5 s in the cache, runs before slow-b, 50 s.
        write(self.repo / "tests/test_metrics.py", STATEMENT)
        write(self.repo / "lib/code.py", "def value():\n    return 2\n")
        self.validate()
        self.assertEqual(self.runs(), ["quick", "quick", "slow-a", "slow-b"])

    def test_a_default_check_reruns_after_a_docstring_only_change(self):
        # Evidence keys stay byte-based: a docstring edit is no reason to reuse a log of other bytes.
        self.config["checks"] = [self.check("quick", ["tests/*.py"], tier="default")]
        self.assertFalse(self.validate()["quick"]["reused"])
        self.assertTrue(self.validate()["quick"]["reused"])
        write(self.repo / "tests/test_metrics.py", DOCSTRINGS)
        record = self.validate()["quick"]
        self.assertEqual((record["status"], record["reused"]), ("passed", False))
        self.assertNotIn("inert", record)
        self.assertEqual(self.runs(), ["quick", "quick"])

    def test_reports_name_the_inert_outcome(self):
        self.config["checks"] = [self.quick(), self.check("slow", ["tests/*.py"])]
        write(self.repo / "tests/test_metrics.py", DOCSTRINGS)
        records = self.validate()
        comment = messages.validation(self.runner.ctx, issue="DEV-1", records=list(records.values()), passed=True,
                                      directory=self.last["validation_dir"])
        self.assertTrue(comment.startswith("Checks passed for DEV-1 (1 check);"), comment)
        self.assertIn("**Not applicable**\nslow was not run: the controller compared 1 changed file its inputs match "
                      "with the issue's starting commit and found every change inert (Python equal without docstrings, "
                      "or documentation no test reads).", comment)
        self.assertEqual(outcome_text(check_status(records["slow"])), "not run (only inert changes to its inputs)")
        self.assertEqual(INERT_OUTCOME, "not run (only inert changes to its inputs)")
        handoff = self.runner.synthesize_handoff(self.last, "implement")
        self.assertIn({"command": "slow", "outcome": INERT_OUTCOME}, handoff["validation"])
        # `report` and `measure` read the saved records.
        self.assertIn(f"| {INERT_OUTCOME} |", trajectory.render_markdown(trajectory.from_roots([self.root / "runs"])))
        self.assertIn(f"| {INERT_OUTCOME} |", measure.render_markdown(measure.measure([self.root / "runs"])))


if __name__ == "__main__":
    unittest.main()
