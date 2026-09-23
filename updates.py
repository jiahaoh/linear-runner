"""Human-review Linear updates: templates, draft lint and hidden event markers.

Two audiences are kept apart. Agent-review contracts are the structured result and
review JSON schemas in ``runner.py``. Human-review updates are the plain-Markdown
templates in ``templates/``: every Linear comment opens with one plain sentence (what
happened and what, if anything, the owner must do), followed by short optional sections
and at most one ``Evidence:`` line of host paths. No JSON, code blocks, tables or hashes.

Workers and reviewers write drafts (``NNN-<kind>.md``) into their attempt's outbox; the
runner lints each draft against its template and posts valid ones as new comments.
Lint limits and the outbox timing are site ``attention`` settings. Runner comments put each
command in its own fenced ``bash`` block after a plain sentence; drafts may not use code blocks.
"""
from __future__ import annotations

from pathlib import Path
import re

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
# Draft kinds a model may write, per phase. ``review`` drafts come from the reviewer's
# structured ``summary`` because the review sandbox is read-only (see Runner.review_draft).
DRAFT_KINDS = {"implement": ("progress", "ready", "blocked"), "repair": ("progress", "ready", "blocked"),
               "review": ("review",)}
# Drafts posted as soon as they are seen; the others wait for the session's result.
IMMEDIATE_KINDS = ("progress",)
DRAFT_NAME = re.compile(r"^(\d{3})-([a-z]+)\.md$")
MARKER_PREFIX = "<!-- linear-runner "

_FIELD = re.compile(r"\{([a-z_]+)\}")
_SECTION = re.compile(r"^(?:\*\*(?P<bold>[^*]+?)\*\*|#{1,4}\s+(?P<hash>.+?))\s*:?\s*$")
_TABLE = re.compile(r"^\s*\|.*\|\s*$|^\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_JSON = re.compile(r'^\s*[\[{].*[\]}]\s*,?$|\{\s*"|"\s*:\s*[\[{"\d]')
_HASH = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_PLACEHOLDER = re.compile(r"^\s*<[^>]{3,}>\s*$")
_SENTENCE_BREAK = re.compile(r"[.!?]\s+\S")


class TemplateError(ValueError):
    pass


# --- Templates ------------------------------------------------------------------

def load_template(name):
    """Return {"meta": {...}, "body": str, "sections": [...]} for templates/<name>.md."""
    path = TEMPLATE_DIR / f"{name}.md"
    text = path.read_text()
    meta = {}
    if text.startswith("---\n"):
        header, _, text = text[4:].partition("\n---\n")
        for line in header.splitlines():
            if line.strip():
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip()
    body = text.strip("\n")
    sections = [section_name(line) for line in body.splitlines() if section_name(line)]
    required = [s.strip() for s in meta.get("required", "").split(",") if s.strip()]
    return {"name": name, "meta": meta, "body": body, "sections": sections, "required": required}


def section_name(line):
    match = _SECTION.match(line)
    if not match:
        return None
    return " ".join((match["bold"] or match["hash"]).split()).rstrip(":").strip()


def fill(text, values):
    return _FIELD.sub(lambda m: str(values.get(m.group(1)) if values.get(m.group(1)) is not None else ""), text)


def variant(name, group, key, values=None):
    """Wording ``<group>.<key>`` from a template's front matter, filled with ``values``."""
    meta = load_template(name)["meta"]
    entry = meta.get(f"{group}.{key}")
    if entry is None:
        raise TemplateError(f"templates/{name}.md has no {group}.{key}")
    return fill(entry, values or {})


def render(name, fields, headline="default"):
    """Fill a runner template. Paragraphs whose fields are all empty are left out."""
    template = load_template(name)
    values = dict(fields)
    if "{headline}" in template["body"]:
        values["headline"] = variant(name, "headline", headline, values)
    blocks = []
    for block in template["body"].split("\n\n"):
        names = _FIELD.findall(block)
        if names and all(not str(values.get(n) or "").strip() for n in names):
            continue
        blocks.append(fill(block, values).strip())
    return "\n\n".join(b for b in blocks if b) + "\n"


# --- Lint ---------------------------------------------------------------------------

def draft_template(kind):
    return load_template("draft-" + kind)


def lint(text, *, kind, limits, sections=None, required=(), allowed_kinds=None, allow_commands=False):
    """Return a list of problems (empty when the text may be posted).

    ``sections`` limits headings to a template's headings; ``required`` must be present.
    ``allow_commands`` (runner comments only) permits fenced ``bash`` blocks of exactly one
    command line each.
    """
    problems = []
    if allowed_kinds is not None and kind not in allowed_kinds:
        problems.append(f"kind {kind!r} is not allowed here; use one of {', '.join(allowed_kinds)}")
    body = (text or "").strip()
    if not body:
        return problems + ["the draft is empty"]
    if len(body) > limits["max_chars"]:
        problems.append(f"too long: {len(body)} characters (limit {limits['max_chars']})")
    lines = body.splitlines()
    if len(lines) > limits["max_lines"]:
        problems.append(f"too many lines: {len(lines)} (limit {limits['max_lines']})")
    first = " ".join(body.split("\n\n")[0].split())
    if section_name(lines[0]) or re.match(r"^\s*([#>|`~-]|\*\s|\d+\.\s|<)", lines[0]):
        problems.append("the first line must be one plain sentence, not a heading, list, quote or placeholder")
    elif not first.endswith((".", "!", "?")) or _SENTENCE_BREAK.search(first[:-1]):
        problems.append("the first paragraph must be exactly one sentence ending with '.', '!' or '?'")
    elif len(first) > limits["max_first_sentence_chars"]:
        problems.append(f"the first sentence is {len(first)} characters (limit {limits['max_first_sentence_chars']})")
    found = []
    evidence = [i for i, line in enumerate(lines) if line.strip().startswith("Evidence:")]
    commands = set()
    if allow_commands:
        index = 0
        while index < len(lines):
            if lines[index].strip() == "```bash":
                end = index + 1
                while end < len(lines) and lines[end].strip() != "```":
                    end += 1
                if end == len(lines) or end - index != 2 or not lines[index + 1].strip():
                    problems.append(f"line {index + 1}: a command block must hold exactly one command line")
                commands.update(range(index, min(end, len(lines) - 1) + 1))
                index = end
            index += 1
    for index, line in enumerate(lines):
        if index in commands:
            continue
        stripped = line.strip()
        name = section_name(line)
        if name:
            found.append(name)
            if sections is not None and name not in sections:
                problems.append(f"unknown section {name!r}; use only: {', '.join(sections)}")
        if stripped.startswith(("```", "~~~")):
            problems.append(f"line {index + 1}: code blocks are not allowed")
        if _TABLE.match(line):
            problems.append(f"line {index + 1}: tables are not allowed")
        if index not in evidence and _JSON.search(stripped):
            problems.append(f"line {index + 1}: JSON or raw records are not allowed")
        if _HASH.search(stripped):
            problems.append(f"line {index + 1}: long hashes are not allowed; put them in artifacts")
        if "<!--" in stripped:
            problems.append(f"line {index + 1}: HTML comments are not allowed")
        if _PLACEHOLDER.match(line):
            problems.append(f"line {index + 1}: a template placeholder was left in")
    if len(evidence) > 1:
        problems.append("at most one 'Evidence:' line is allowed")
    elif evidence and evidence[0] != len(lines) - 1:
        problems.append("the 'Evidence:' line must be the last line")
    missing = [s for s in required if s not in found]
    if missing:
        problems.append(f"missing required section(s): {', '.join(missing)}")
    return list(dict.fromkeys(problems))


def lint_draft(path, phase, limits):
    """Lint an outbox draft file; return (kind, text, problems)."""
    path = Path(path)
    match = DRAFT_NAME.match(path.name)
    if not match:
        return None, "", [f"file name {path.name!r} is not NNN-<kind>.md"]
    kind = match.group(2)
    text = path.read_text(errors="replace")
    allowed = DRAFT_KINDS.get(phase, ())
    if kind not in allowed:
        return kind, text, [f"kind {kind!r} is not allowed in the {phase} phase; use one of {', '.join(allowed)}"]
    template = draft_template(kind)
    return kind, text, lint(text, kind=kind, limits=limits, sections=template["sections"], required=template["required"])


# --- Markers ------------------------------------------------------------------------

def event_key(issue, kind, seq):
    return f"{issue}/{kind}/{seq}"


def marker(batch_id, key):
    """One hidden line identifying an event comment; used only to reconcile a lost write."""
    return f"{MARKER_PREFIX}{batch_id}/{key} -->"


def with_marker(body, batch_id, key):
    return body.rstrip() + "\n\n" + marker(batch_id, key)


def plain(text, limit=700):
    """Model text quoted in a runner comment: no fences, tables, markers; bounded length."""
    lines = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~", "<!--")) or _TABLE.match(line) or _HASH.search(stripped):
            continue
        lines.append(line.rstrip())
    value = "\n".join(lines).strip()
    value = re.sub(r"\n{3,}", "\n\n", value)
    if len(value) > limit:
        value = value[:limit].rsplit(" ", 1)[0].rstrip(",;:") + " …"
    return value


def quote(text):
    """Quote model-written prose as one Markdown block quote (paragraph breaks kept)."""
    value = plain(text)
    return "\n".join("> " + line if line else ">" for line in value.splitlines()) if value else ""


def first_sentence(text):
    value = " ".join(str(text or "").split())
    match = re.search(r"[.!?](\s|$)", value)
    return value[:match.end()].strip() if match else value


# --- Event ledger ---------------------------------------------------------------------

class Ledger:
    """Append-only, exactly-once Linear events keyed by (issue, kind, sequence).

    ``store()`` returns the dict that holds ``events`` and ``event_counters`` (the runner's
    state, or the watchdog's own record); ``save()`` persists it. Each event is saved as
    pending before the write and as posted after the read-back. A pending event (a write
    whose response or local save was lost) is reconciled by its hidden marker: adopted if
    the comment exists, posted otherwise; comments are never edited. ``dedupe`` names the
    occurrence (a run directory, block, draft file, ...) so re-running the same step after
    a crash finds its event instead of starting a new one.
    """

    def __init__(self, store, save, linear, batch_id, log=None):
        self.store, self.save, self.linear, self.batch_id = store, save, linear, batch_id
        self.log = log or (lambda message: None)

    def find(self, issue, kind, dedupe):
        for record in self.store().get("events", {}).values():
            if record["issue"] == issue and record["kind"] == kind and record.get("dedupe") == dedupe:
                return record
        return None

    def emit(self, issue, kind, body, *, dedupe=None, now=None):
        if dedupe is not None:
            existing = self.find(issue, kind, dedupe)
            if existing:
                return self.deliver(existing) if existing["status"] == "pending" else existing
        state = self.store()
        counters = state.setdefault("event_counters", {})
        seq = counters.get(f"{issue}/{kind}", 0) + 1
        counters[f"{issue}/{kind}"] = seq
        key = event_key(issue, kind, seq)
        record = {"key": key, "issue": issue, "kind": kind, "seq": seq, "dedupe": dedupe, "status": "pending",
                  "attempts": 0, "body": with_marker(body, self.batch_id, key), "recorded_at": now}
        state.setdefault("events", {})[key] = record
        self.save()
        return self.deliver(record)

    def deliver(self, record):
        record["attempts"] += 1
        self.save()
        identity = self.linear.post_comment(record["issue"], record["body"], marker(self.batch_id, record["key"]),
                                            reconcile=record["attempts"] > 1)
        record.update(status="posted", comment_id=identity)
        self.save()
        self.log(f"Linear: posted {record['kind']} on {record['issue']} ({record['key']})")
        return record

    def pending(self):
        return [r for r in self.store().get("events", {}).values() if r["status"] == "pending"]

    def reconcile(self):
        for record in self.pending():
            self.deliver(record)
