"""The Linear issue template generated from templates/issue-contract.md (W-206)."""
import contextlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from linear_runner import cli
from linear_runner.config import load_config, load_registry
from linear_runner.linear import template
from linear_runner.version import RELEASE
from tests.fixtures import CHECKOUT, make_home


class TemplateLinear:
    """Linear's template tools as the MCP endpoint offers them: list and read only."""

    def __init__(self, templates):
        self.templates = templates
        self.calls = []

    def call(self, name, **args):
        self.calls.append((name, args))
        if name == "list_templates":
            return {"templates": [{"id": t["id"], "name": t["name"], "type": "issue"} for t in self.templates]}
        if name == "get_template":
            return next(dict(t) for t in self.templates if t["id"] == args["id"])
        raise AssertionError(f"unexpected Linear tool {name}")


class RenderTests(unittest.TestCase):
    def rendered(self):
        policy, _, _ = load_registry(CHECKOUT / "examples" / "home")
        return template.render(policy["labels"], RELEASE)

    def test_the_dry_run_renders_the_repository_file(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["sync-linear-template", "--dry-run", "--home", str(CHECKOUT / "examples" / "home")])
        text = out.getvalue()
        self.assertTrue(text.startswith("Name: Runner issue contract\nLabels: one task kind (Research | Implementation "
                                        "| Validation | Maintenance) and one profile (Deep | Standard | Economy)\n"))
        source = (CHECKOUT / "templates" / "issue-contract.md").read_text()
        body = source.split("\n---\n", 1)[1].strip()
        self.assertIn(body, text)  # every section, verbatim
        self.assertNotIn("used-for:", text)  # the front matter is not part of the template
        for heading in re.findall(r"^## .+$", source, re.M):
            self.assertIn(heading, text)
        self.assertIn("Every acceptance criterion names where its evidence comes from.", text)
        self.assertIn(f"(linear-runner {RELEASE})", text)
        self.assertEqual(text, template.text_of(self.rendered()))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home, self.batch = make_home(self.root, self.root / "repo")
        self.workspace = self.home / "workspaces" / "test.json"
        self.rendered = RenderTests.rendered(self)

    def test_running_twice_records_one_template_in_place(self):
        linear = TemplateLinear([{"id": "tpl-1", "name": template.NAME, "description": self.rendered["body"]},
                                 {"id": "tpl-2", "name": "Bug report", "description": "x"}])
        first = template.sync(linear, self.workspace, self.rendered)
        before = self.workspace.read_bytes()
        second = template.sync(linear, self.workspace, self.rendered)
        self.assertEqual((first["status"], first["id"], first["recorded"]), ("current", "tpl-1", True))
        self.assertEqual((second["status"], second["recorded"]), ("current", False))
        self.assertEqual(self.workspace.read_bytes(), before)  # idempotent
        self.assertEqual(json.loads(before)["issue_template"], {"name": template.NAME, "id": "tpl-1"})
        self.assertEqual({name for name, _ in linear.calls}, {"list_templates", "get_template"})  # nothing created
        self.assertEqual(len(linear.templates), 2)
        self.assertEqual(load_config(self.batch, self.home)["linear_workspace"], "test")  # the key is allowed

    def test_an_outdated_or_missing_template_names_the_manual_step(self):
        linear = TemplateLinear([{"id": "tpl-1", "name": template.NAME, "content": "old text"}])
        report = template.sync(linear, self.workspace, self.rendered)
        self.assertEqual(report["status"], "differs")
        self.assertIn("update the issue template \"Runner issue contract\"", report["action"])
        missing = template.sync(TemplateLinear([]), self.workspace, self.rendered)
        self.assertEqual(missing["status"], "missing")
        self.assertIn("create the issue template", missing["action"])
        both = TemplateLinear([{"id": "a", "name": template.NAME, "content": ""}, {"id": "b", "name": template.NAME}])
        self.assertEqual(template.sync(both, self.workspace, self.rendered)["ids"], ["a", "b"])

    def test_the_command_line_reports_and_fails_until_the_template_is_current(self):
        args = ["sync-linear-template", "--home", str(self.home), "--workspace", "test"]
        linear = TemplateLinear([])
        with patch.object(cli, "LinearClient", return_value=linear), contextlib.redirect_stdout(io.StringIO()) as out, \
                self.assertRaises(SystemExit) as exit_:
            cli.main(args)
        self.assertEqual((exit_.exception.code, json.loads(out.getvalue())["status"]), (1, "missing"))
        self.assertNotIn("issue_template", json.loads(self.workspace.read_text()))
        linear.templates.append({"id": "tpl-9", "name": template.NAME, "description": self.rendered["body"]})
        with patch.object(cli, "LinearClient", return_value=linear), contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(args + ["--team", "Team"])
        self.assertEqual(json.loads(out.getvalue())["status"], "current")
        self.assertEqual(linear.calls[-2], ("list_templates", {"type": "issue", "team": "Team"}))
        self.assertEqual(json.loads(self.workspace.read_text())["issue_template"]["id"], "tpl-9")
