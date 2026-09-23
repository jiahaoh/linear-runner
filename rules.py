"""Pre-authorized decision rules written in an issue description.

An issue may carry at most one fenced code block whose info string is exactly
``linear-runner-rules``. Each non-blank line that does not start with ``#`` is one rule::

    ```linear-runner-rules
    # defer the viewer issue instead of stopping everything
    defer issue when worker blocked 2 times on the same criterion
    stop batch when review blocked 1 time
    ```

Grammar (keywords are case-insensitive; quoted criterion text must match exactly)::

    <action> when <event> <N> time|times [on the same criterion | on "<exact criterion text>"]

    action: defer issue | stop batch
    event:  worker blocked | review blocked
    N:      a whole number >= 1; "1 time", "2 times", ...

Any other line is an error that names the line, and the supervisor refuses to dispatch
the issue (launch preflight fails). Rules are read from the issue as pinned at intake,
never from a later live edit. A rule applies only when its exact condition matches the
issue's recorded blocks; the first matching rule in order wins, and every application
is recorded with the rule's id (derived from its normalized text). Rules can only defer
the issue or stop the batch; they never accept work, drop a criterion or change scope.
"""
from __future__ import annotations

import hashlib
import re

FENCE = "linear-runner-rules"
ACTIONS = {"defer issue": "defer_issue", "stop batch": "stop_batch"}
EVENTS = {"worker blocked": "worker_blocked", "review blocked": "review_blocked"}
GRAMMAR = '<action> when <event> <N> time(s) [on the same criterion | on "<exact criterion text>"]'
_BLOCK = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*" + re.escape(FENCE) + r"[ \t]*\n(.*?)\n?[ \t]*\1[ \t]*$",
                    re.M | re.S)
_RULE = re.compile(r"(?P<action>defer\s+issue|stop\s+batch)\s+when\s+(?P<event>worker\s+blocked|review\s+blocked)"
                   r"\s+(?P<count>\d+)\s+(?P<unit>times?)"
                   r"(?:\s+on\s+(?:(?P<same>the\s+same\s+criterion)|\"(?P<criterion>.+)\"))?", re.I)


class RuleError(ValueError):
    pass


def _words(text):
    return " ".join(text.lower().split())


def parse_rule(line, number=None, criteria=None):
    """Parse one rule line; raise RuleError naming the line when it does not fit the grammar."""
    where = f"{FENCE} line {number}" if number is not None else FENCE
    text = line.strip()
    match = _RULE.fullmatch(text)
    if not match:
        raise RuleError(f"{where}: {text!r} is not a rule; expected: {GRAMMAR}")
    count = int(match["count"])
    unit = match["unit"].lower()
    if count < 1:
        raise RuleError(f"{where}: {text!r}: the count must be at least 1")
    if unit != ("time" if count == 1 else "times"):
        raise RuleError(f"{where}: {text!r}: write '1 time' or '{max(count, 2)} times'")
    action, event = _words(match["action"]), _words(match["event"])
    normalized = f"{action} when {event} {count} {unit}"
    when = {"event": EVENTS[event], "min_count": count}
    if match["same"]:
        when["same_criterion"] = True
        normalized += " on the same criterion"
    elif match["criterion"] is not None:
        criterion = match["criterion"]
        if criteria is not None and criterion not in criteria:
            raise RuleError(f"{where}: {text!r}: \"{criterion}\" is not an unchecked criterion of this issue "
                            "(the quoted text must match exactly)")
        when["criterion"] = criterion
        normalized += f' on "{criterion}"'
    return {"id": "rule-" + hashlib.sha256(normalized.encode()).hexdigest()[:12], "text": normalized,
            "when": when, "then": {"action": ACTIONS[action]}}


def parse_rules(description, criteria=None):
    """Return the validated rule list ([] when the description has no rule block)."""
    blocks = _BLOCK.findall(description or "")
    if not blocks:
        if FENCE in (description or ""):
            raise RuleError(f"'{FENCE}' is mentioned but no well-formed fenced block was found")
        return []
    if len(blocks) > 1:
        raise RuleError(f"more than one '{FENCE}' block")
    rules, seen = [], set()
    for number, line in enumerate(blocks[0][1].splitlines(), start=1):
        if not line.strip() or line.strip().startswith("#"):
            continue
        rule = parse_rule(line, number, criteria)
        if rule["id"] in seen:
            raise RuleError(f"{FENCE} line {number}: {line.strip()!r} repeats an earlier rule")
        seen.add(rule["id"])
        rules.append(rule)
    return rules


def evaluate(rules, blocks):
    """First rule whose exact condition matches the issue's recorded blocks, or None.

    ``blocks`` are the supervisor's records for one issue, oldest first; each has ``id``,
    ``event`` and ``unsatisfied`` (criterion texts the model reported unsatisfied).
    The condition looks at the latest ``N`` blocks of the named event.
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
            if not common:
                continue
            matched = sorted(common)
        return {"rule": rule, "blocks": [b["id"] for b in window], "matched_criteria": matched}
    return None
