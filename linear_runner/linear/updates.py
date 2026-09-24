"""Human-review Linear updates: templates, draft lint and hidden event markers.

Two audiences are kept apart. Agent-review contracts are the structured result and
review JSON schemas in ``runner.py``. Human-review updates are the plain-Markdown
templates in ``templates/``: every Linear comment opens with one plain sentence (what
happened and what, if anything, the owner must do), followed by short optional sections
and at most one ``Evidence:`` line of host paths. No JSON, code blocks, tables or hashes;
runner comments may add commands (see below) and the run summary's usage tables.

Workers and reviewers write drafts (``NNN-<kind>.md``) into their attempt's outbox; the
runner lints each draft against its template and posts valid ones as new comments.
Lint limits and the outbox timing are site ``attention`` settings. Runner comments put each
command in its own fenced ``bash`` block after a plain sentence; drafts may not use code blocks.
"""
from __future__ import annotations

from pathlib import Path
import re

from linear_runner.config import RUNNER_ROOT

TEMPLATE_DIR = RUNNER_ROOT / "templates"
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
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_DELIMITER = re.compile(r"^\s*:?-{3,}:?\s*$")
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


def table_cells(line):
    """The cells of one Markdown table row (``\\|`` is a literal bar inside a cell)."""
    inner = line.strip()[1:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", inner)]


def tables(lines):
    """``(indices, problems)`` for the Markdown table blocks in ``lines``: runs of ``| ... |``
    rows. A table has a header row, a delimiter row (``|---|---|``) and rows with the same
    number of cells, and stands in its own paragraph."""
    indices, problems = set(), []
    index = 0
    while index < len(lines):
        if not _TABLE_ROW.match(lines[index]):
            index += 1
            continue
        end = index
        while end < len(lines) and _TABLE_ROW.match(lines[end]):
            end += 1
        block = lines[index:end]
        width = len(table_cells(block[0]))
        if len(block) < 3 or not all(_TABLE_DELIMITER.match(cell) for cell in table_cells(block[1])):
            problems.append(f"line {index + 1}: a table needs a header row, a delimiter row and at least one row")
        elif any(len(table_cells(row)) != width for row in block[1:]):
            problems.append(f"line {index + 1}: every table row needs {width} cells")
        if (index and lines[index - 1].strip()) or (end < len(lines) and lines[end].strip()):
            problems.append(f"line {index + 1}: a table must be its own paragraph (blank lines around it)")
        indices.update(range(index, end))
        index = end
    return indices, problems


def lint(text, *, kind, limits, sections=None, required=(), allowed_kinds=None, allow_commands=False,
         allow_tables=False):
    """Return a list of problems (empty when the text may be posted).

    ``sections`` limits headings to a template's headings; ``required`` must be present.
    ``allow_commands`` (runner comments only) permits fenced ``bash`` blocks of exactly one
    command line each. ``allow_tables`` (runner comments only: the run summary's usage
    tables) permits well-formed Markdown tables; their lines do not count against
    ``max_chars`` and ``max_lines``, which bound prose, and they are still checked for JSON,
    long hashes and HTML comments. Model drafts never get either.
    """
    problems = []
    if allowed_kinds is not None and kind not in allowed_kinds:
        problems.append(f"kind {kind!r} is not allowed here; use one of {', '.join(allowed_kinds)}")
    body = (text or "").strip()
    if not body:
        return problems + ["the draft is empty"]
    lines = body.splitlines()
    table_lines, table_problems = tables(lines) if allow_tables else (set(), [])
    problems += table_problems
    prose = len(body) - sum(len(lines[i]) + 1 for i in table_lines)
    if prose > limits["max_chars"]:
        problems.append(f"too long: {prose} characters (limit {limits['max_chars']})")
    if len(lines) - len(table_lines) > limits["max_lines"]:
        problems.append(f"too many lines: {len(lines) - len(table_lines)} (limit {limits['max_lines']})")
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
        if _TABLE.match(line) and index not in table_lines:
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


def draft_rules(limits, kinds):
    """The draft rules as model prompts state them, built from the same ``limits`` that ``lint``
    checks and from each kind's template (its required sections), so the two cannot drift.
    The first-paragraph rule is spelled out with an example because two short sentences
    ("X is ready. No action is needed.") were the W-191 canary's rejected review summary."""
    required = "; ".join(f"{kind}: " + ", ".join(f"**{name}**" for name in draft_template(kind)["required"])
                         for kind in kinds)
    return ("(1) the first paragraph is exactly ONE sentence, at most "
            f"{limits['max_first_sentence_chars']} characters, ending with '.', '!' or '?', that says what happened "
            "and whether the owner must act. Nothing else goes before the first blank line: no second sentence "
            "and no abbreviation with a period followed by a space (such as 'e.g. '). For example, not \"X is "
            "ready. No action is needed.\" but \"X is ready and the owner does not need to act.\" (2) After a "
            "blank line, use only the template's section headings, each in bold on its own line; required "
            f"sections: {required}. (3) Plain sentences only: no JSON, code blocks, tables, HTML comments or long "
            f"hashes. (4) At most {limits['max_chars']} characters and {limits['max_lines']} lines in total. (5) An "
            "optional last line 'Evidence: <host paths, comma separated>'")


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


# Linear turns a bare issue identifier (``TEAM-123``) or issue URL in a comment or description
# into a link and adds a "related" relation between the two issues, which it re-creates from
# description mentions after removal. Linear is assumed not to link text inside a Markdown
# code span; this cannot be verified offline. Runner comments that quote people's or models'
# words (recovery reasons, owner notes, summaries, error text) therefore show such mentions as
# inline code, through neutralize_issue_mentions only.
_MENTION = re.compile(r"(?P<url>https?://linear\.app/[^\s`]*[^\s`.,;:!?)\]])"
                      r"|(?<![\w/.-])(?P<id>[A-Z][A-Z0-9]{0,9}-\d+)(?![\w-])")
_CODE_SPAN = re.compile(r"(`[^`\n]*`)")


def issue_mentions(text):
    """Bare Linear issue identifiers and Linear URLs in ``text`` outside code spans, in order."""
    found = []
    for index, part in enumerate(_CODE_SPAN.split(str(text or ""))):
        if index % 2 == 0:
            found += [m.group(0) for m in _MENTION.finditer(part)]
    return list(dict.fromkeys(found))


def neutralize_issue_mentions(text):
    """``text`` with every bare issue identifier and Linear URL outside a code span wrapped in
    inline code, so posting it does not link issues in Linear. Idempotent."""
    parts = _CODE_SPAN.split(str(text or ""))
    return "".join(part if index % 2 else _MENTION.sub(lambda m: f"`{m.group(0)}`", part)
                   for index, part in enumerate(parts))


def plain(text, limit=700):
    """Model or owner text quoted in a runner comment: no fences, tables, markers; bounded
    length; issue mentions neutralized (see neutralize_issue_mentions)."""
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
    return neutralize_issue_mentions(value)


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
