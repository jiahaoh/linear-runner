"""Criterion wording a project lists as ambiguous (W-345).

Reviews read acceptance criteria literally. In s29-amend-20261005 "the controller's complete
suite" passed review at W-336 and W-337 and blocked W-338, and "A19 hash lists unchanged"
blocked W-336; earlier batches lost repairs to "every array" and "measure every call". A
project's ``criterion_lint`` rules name such wording with a suggested rewrite. The launch
preflight and ``dry-run`` list each match in an unclaimed issue as a warning; nothing stops.
"""
from __future__ import annotations

import re

from linear_runner.engine import intake


def compile_rules(rules):
    """``[(regex, rule)]`` for the project's ``criterion_lint`` rules (case-insensitive)."""
    return [(re.compile(rule["pattern"], re.IGNORECASE), rule) for rule in rules or []]


def lint_issue(issue, rules):
    """Warnings for one issue: each criterion (numbered among its unchecked criteria) that
    matches a rule and does not contain the rule's ``unless`` phrase."""
    warnings = []
    for number, criterion in enumerate(intake.unchecked_criteria(issue.get("description", "")), start=1):
        for regex, rule in rules:
            match = regex.search(criterion)
            if not match or (rule.get("unless") and rule["unless"].lower() in criterion.lower()):
                continue
            warnings.append(f"{issue['id']} criterion {number} says \"{match.group(0)}\": {rule['message']}")
    return warnings


def lint_issues(issues, rules, claimed=()):
    """Warnings for every issue not claimed and not Done, in the order given."""
    compiled = compile_rules(rules)
    if not compiled:
        return []
    return [warning for issue in issues
            if issue["id"] not in claimed and issue.get("statusType") != "completed"
            for warning in lint_issue(issue, compiled)]
