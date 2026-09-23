"""DRAFT: pre-authorized decision rules written in an issue description.

An issue may carry at most one fenced code block whose info string is exactly
``linear-runner-rules``. The block holds one JSON object::

    ```linear-runner-rules
    {"version": 1,
     "rules": [{"id": "defer-after-two-blocks",
                "when": {"event": "worker_blocked", "min_count": 2, "same_criterion": true},
                "then": {"action": "defer_issue"}}]}
    ```

Rules are read from the issue as pinned at intake (the contract), never from a later
live edit. Parsing is strict: unknown keys, events or actions reject the whole block, and
the supervisor refuses to dispatch an issue whose block does not parse. A rule applies
only when its exact condition matches the recorded blocks of that issue; the first
matching rule in order wins and every application is recorded by the caller. Rules can
only stop or defer; they never accept, skip a criterion or change scope.
"""
from __future__ import annotations

import json
import re

FENCE = "linear-runner-rules"
VERSION = 1
# Block events recorded by the supervisor. Only the first two may appear in a rule.
EVENTS = ("worker_blocked", "review_blocked")
ACTIONS = ("defer_issue", "stop")
_BLOCK = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*" + re.escape(FENCE) + r"[ \t]*\n(.*?)\n[ \t]*\1[ \t]*$",
                    re.M | re.S)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class RuleError(ValueError):
    pass


def _keys(value, allowed, required, where):
    if not isinstance(value, dict):
        raise RuleError(f"{where}: expected an object")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise RuleError(f"{where}: unknown key(s) {unknown}")
    missing = sorted(set(required) - set(value))
    if missing:
        raise RuleError(f"{where}: missing key(s) {missing}")


def parse_rules(description, criteria=None):
    """Return the validated rule list ([] when the description has no rule block)."""
    blocks = _BLOCK.findall(description or "")
    if not blocks:
        if FENCE in (description or ""):
            raise RuleError(f"'{FENCE}' is mentioned but no well-formed fenced block was found")
        return []
    if len(blocks) > 1:
        raise RuleError(f"more than one '{FENCE}' block")
    try:
        data = json.loads(blocks[0][1])
    except ValueError as error:
        raise RuleError(f"rule block is not valid JSON: {error}") from None
    _keys(data, ("version", "rules"), ("version", "rules"), "rules")
    if data["version"] != VERSION:
        raise RuleError(f"rules.version must be {VERSION}")
    if not isinstance(data["rules"], list) or not data["rules"]:
        raise RuleError("rules.rules must be a non-empty list")
    seen = set()
    for index, rule in enumerate(data["rules"]):
        where = f"rules[{index}]"
        _keys(rule, ("id", "when", "then"), ("id", "when", "then"), where)
        if not isinstance(rule["id"], str) or not _ID.match(rule["id"]) or rule["id"] in seen:
            raise RuleError(f"{where}.id: must be a unique identifier")
        seen.add(rule["id"])
        when = rule["when"]
        _keys(when, ("event", "min_count", "same_criterion", "criterion"), ("event", "min_count"), f"{where}.when")
        if when["event"] not in EVENTS:
            raise RuleError(f"{where}.when.event: must be one of {list(EVENTS)}")
        if not isinstance(when["min_count"], int) or isinstance(when["min_count"], bool) or when["min_count"] < 1:
            raise RuleError(f"{where}.when.min_count: must be an integer >= 1")
        if not isinstance(when.get("same_criterion", False), bool):
            raise RuleError(f"{where}.when.same_criterion: must be true or false")
        if "criterion" in when:
            if not isinstance(when["criterion"], str) or not when["criterion"]:
                raise RuleError(f"{where}.when.criterion: must be non-empty text")
            if criteria is not None and when["criterion"] not in criteria:
                raise RuleError(f"{where}.when.criterion: not an unchecked criterion of this issue")
        _keys(rule["then"], ("action",), ("action",), f"{where}.then")
        if rule["then"]["action"] not in ACTIONS:
            raise RuleError(f"{where}.then.action: must be one of {list(ACTIONS)}")
    return data["rules"]


def evaluate(rules, blocks):
    """First rule whose exact condition matches the issue's recorded blocks, or None.

    ``blocks`` are the supervisor's records for one issue, oldest first; each has ``id``,
    ``event`` and ``unsatisfied`` (criterion texts the model reported unsatisfied).
    The condition looks at the latest ``min_count`` blocks of the named event.
    """
    for rule in rules:
        when = rule["when"]
        events = [b for b in blocks if b["event"] == when["event"]]
        if len(events) < when["min_count"]:
            continue
        window = events[-when["min_count"]:]
        matched = None
        if "criterion" in when:
            if not all(when["criterion"] in b["unsatisfied"] for b in window):
                continue
            matched = [when["criterion"]]
        if when.get("same_criterion"):
            common = set.intersection(*(set(b["unsatisfied"]) for b in window))
            if matched is not None:
                common &= set(matched)
            if not common:
                continue
            matched = sorted(common)
        return {"rule": rule, "blocks": [b["id"] for b in window], "matched_criteria": matched}
    return None
