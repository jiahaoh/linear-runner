"""Small authenticated Streamable HTTP MCP client; no model or credential logging."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request


# Linear may acknowledge a write before a read shows it: In Review read one second after the
# write, a label and a new comment each missing on the first read (W-251). A read-back that
# does not yet show the write is repeated after these pauses (about 10 s in all) before it
# fails. Only the read is repeated; the write is never re-sent inside the retry.
READ_BACK_DELAYS = (0.5, 1, 1.5, 2, 2.5, 2.5)
SLEEP = time.sleep  # the pause between reads (the test package makes it a no-op)


def read_back(read, accepted, *, delays=None, sleep=None):
    """``read()`` until ``accepted(value)``, pausing READ_BACK_DELAYS between reads; return the
    last value read (accepted or not: the caller raises its own error)."""
    value = read()
    for delay in READ_BACK_DELAYS if delays is None else delays:
        if accepted(value):
            break
        (sleep or SLEEP)(delay)
        value = read()
    return value


# A Linear 5xx or "temporarily unavailable" answer (the 2.2.0 canary hit a 502
# ``upstream_unavailable`` on a comment write) is retried after these pauses. Every tool but
# ``save_comment`` is a read or an idempotent write (a state, a full label set, a description),
# so ``call`` repeats it; a comment is retried by ``append_comment``, which first looks for the
# comment's marker so a write that did land is never posted twice.
TRANSIENT_DELAYS = (2, 5)
_TRANSIENT = ("upstream_unavailable", "temporarily unavailable", '"status":500', '"status":502', '"status":503',
              '"status":504')


class LinearTransient(RuntimeError):
    """Linear answered that it is temporarily unavailable (a 5xx); the message says so."""


# Workspace ``auth.refresh_command`` overrides it. A short Codex session starts the configured
# Linear MCP server, which refreshes the Codex-owned credential (W-251).
DEFAULT_REFRESH_COMMAND = "codex exec --skip-git-repo-check 'Reply with OK.'"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("MCP redirect refused; verify the configured endpoint")


class LinearClient:
    def __init__(self, config):
        self.config = config
        self.url = config.get("url", "https://mcp.linear.app/mcp")
        if self.url != "https://mcp.linear.app/mcp":
            raise ValueError("Only the official Linear MCP endpoint is supported")
        self.session = None
        self.protocol = "2025-06-18"
        self.sequence = 0
        self.initialized = False
        self.opener = urllib.request.build_opener(NoRedirect())

    def refresh_command(self):
        """How the operator refreshes the OAuth credential (workspace ``auth.refresh_command``)."""
        return self.config.get("refresh_command") or DEFAULT_REFRESH_COMMAND

    def credential(self):
        """The Codex-owned OAuth credential for the endpoint and its expiry in epoch seconds
        (0 when it records none)."""
        path = Path(self.config["credentials_file"]).expanduser()
        entries = json.loads(path.read_text())
        matches = [v for v in entries.values() if isinstance(v, dict)
                   and v.get("server_name") == "linear" and v.get("server_url") == self.url]
        if len(matches) != 1:
            raise RuntimeError("Expected one Linear OAuth credential for the configured endpoint")
        expiry = matches[0].get("expires_at") or 0
        if expiry > 100_000_000_000:  # Codex's file backend records milliseconds.
            expiry /= 1000
        return matches[0], expiry

    def credential_lifetime(self, clock=time.time):
        """Seconds until the OAuth credential expires; None when it cannot be read (a
        ``token_env`` token, or a credential without an expiry). The token is never returned."""
        if self.config.get("token_env"):
            return None
        _, expiry = self.credential()
        return expiry - clock() if expiry else None

    def token(self):
        """Reread credentials so refresh by the owning CLI is visible; never refresh secretly."""
        if self.config.get("token_env"):
            token = os.environ.get(self.config["token_env"])
            if not token:
                raise RuntimeError("Configured Linear bearer-token environment variable is missing")
            return token
        credential, expiry = self.credential()
        if expiry and expiry <= time.time() + 30:
            expired = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(expiry))
            raise RuntimeError(f"Linear OAuth expired at {expired}; refresh it with `{self.refresh_command()}` "
                               "(the owning CLI refreshes a credential once it has expired), then resume")
        return credential["access_token"]

    def rpc(self, method, params=None, notification=False):
        self.sequence += 1
        identity = self.sequence
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            message["id"] = identity
        headers = {"Authorization": "Bearer " + self.token(), "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": self.protocol}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(self.url, data=json.dumps(message).encode(), headers=headers)
        try:
            with self.opener.open(request, timeout=self.config.get("timeout_seconds", 30)) as response:
                self.session = response.headers.get("Mcp-Session-Id", self.session)
                if notification:
                    response.read()
                    return None
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    data = []
                    for raw in response:
                        line = raw.decode().rstrip("\r\n")
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif not line and data:
                            item = json.loads("\n".join(data)); data = []
                            if item.get("id") == identity:
                                break
                    else:
                        raise RuntimeError("MCP stream ended without the matching response")
                else:
                    item = json.load(response)
                if item.get("id") != identity or item.get("error"):
                    raise RuntimeError("Linear MCP protocol request failed; no automatic mutation retry")
                return item["result"]
        except urllib.error.HTTPError as error:
            if error.code >= 500:
                raise LinearTransient(f"Linear temporarily unavailable (HTTP {error.code}); reconcile the write "
                                      "outcome before resume") from None
            raise RuntimeError(f"Linear HTTP {error.code}; reconcile authentication/write outcome before resume") from None

    def call(self, name, **arguments):
        if not self.initialized:
            result = self.rpc("initialize", {"protocolVersion": self.protocol, "capabilities": {},
                                             "clientInfo": {"name": "linear-codex-runner", "version": "2"}})
            self.protocol = result["protocolVersion"]
            self.rpc("notifications/initialized", notification=True)
            self.initialized = True
        pauses = iter(TRANSIENT_DELAYS if name != "save_comment" else ())
        while True:
            try:
                result = self.tool(name, arguments)
                break
            except LinearTransient:
                pause = next(pauses, None)
                if pause is None:
                    raise
                SLEEP(pause)
        texts = [part["text"] for part in result.get("content", []) if part.get("type") == "text"]
        try:
            return json.loads("\n".join(texts))
        except ValueError:
            raise RuntimeError(f"Linear {name} returned unexpected non-JSON content") from None

    def tool(self, name, arguments):
        """One ``tools/call``; an error result raises (``LinearTransient`` when Linear says it is
        temporarily unavailable)."""
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            detail = " ".join(part.get("text", "") for part in result.get("content", [])
                              if part.get("type") == "text").strip()[:300]
            if any(mark in detail.replace(" ", "") or mark in detail for mark in _TRANSIENT):
                raise LinearTransient(f"Linear temporarily unavailable: {name} failed ({detail}); "
                                      "inspect the issue before retrying")
            raise RuntimeError(f"Linear {name} failed ({detail or 'no detail'}); inspect the issue before retrying")
        return result

    def issue(self, identifier):
        return self.call("get_issue", id=identifier, includeRelations=True)

    def comments(self, identifier):
        comments, cursor = [], None
        while True:
            args = {"issueId": identifier, "limit": 250}
            if cursor:
                args["cursor"] = cursor
            page = self.call("list_comments", **args)
            if isinstance(page, list):
                return page
            comments.extend(page["comments"])
            cursor = page.get("cursor") or page.get("nextCursor") or page.get("pageInfo", {}).get("endCursor")
            if not page.get("hasNextPage", page.get("pageInfo", {}).get("hasNextPage", False)):
                return comments
            if not cursor:
                raise RuntimeError("Cannot reconcile paginated Linear comments")

    def _pages(self, tool, key, **arguments):
        items, cursor = [], None
        while True:
            # list_projects and list_users reject limits above 50.
            args = dict(arguments, limit=50)
            if cursor:
                args["cursor"] = cursor
            page = self.call(tool, **args)
            if isinstance(page, list):
                return items + page
            items.extend(page.get(key) or page.get("nodes") or [])
            cursor = page.get("cursor") or page.get("nextCursor") or page.get("pageInfo", {}).get("endCursor")
            if not page.get("hasNextPage", page.get("pageInfo", {}).get("hasNextPage", False)):
                return items
            if not cursor:
                raise RuntimeError(f"Cannot read paginated Linear {tool} results")

    @staticmethod
    def _single(matches, kind, name):
        if len(matches) != 1:
            problem = "no exact match" if not matches else f"ambiguous: {len(matches)} exact matches"
            raise RuntimeError(f"Cannot resolve Linear {kind} {name!r} ({problem}); fix the configured name")
        # Projects carry a short `id` (e.g. "P-TEAM-12") and the UUID that issues reference
        # as `projectId` in `uuid`; users carry the UUID in `id`.
        identity = matches[0].get("uuid") or matches[0].get("id")
        if not isinstance(identity, str) or not identity:
            raise RuntimeError(f"Linear {kind} {name!r} response has no ID")
        return identity

    def resolve_project(self, name):
        """Exact project name -> ID; ambiguity or no match is an error."""
        projects = self._pages("list_projects", "projects", query=name)
        return self._single([p for p in projects if isinstance(p, dict) and p.get("name") == name], "project", name)

    def resolve_user(self, name):
        """'me' -> the authenticated user; otherwise an exact name, display name or email."""
        if name == "me":
            user = self.call("get_user", query="me")
            return self._single([user] if isinstance(user, dict) else [], "user", name)
        users = self._pages("list_users", "users", query=name)
        return self._single([u for u in users if isinstance(u, dict) and name in
                             (u.get("name"), u.get("displayName"), u.get("email"))], "user", name)

    def post_comment(self, issue, body, marker, *, reconcile=False):
        """Append one NEW comment (never edits); see ``append_comment``."""
        return append_comment(self.comments, lambda text: self.call("save_comment", issueId=issue, body=text),
                              issue, body, marker, reconcile=reconcile)


def append_comment(list_comments, create, issue, body, marker, *, reconcile=False):
    """Post ``body`` (which already ends with its hidden ``marker`` line) as a new comment.

    With ``reconcile`` (a previous attempt may have written it before its response or the
    local save was lost), an existing comment carrying the marker is adopted instead of
    posting again. Existing comments are never edited. The comment is read back (``read_back``:
    a comment not listed yet is looked for again; it is never posted twice).
    """
    def marked():
        matches = [c for c in list_comments(issue) if marker in (c.get("body") or "")]
        if len(matches) > 1:
            raise RuntimeError(f"Duplicate event marker on {issue}; reconcile manually")
        return matches[0]["id"] if matches else None

    identity = marked() if reconcile else None
    if identity is None:
        pauses = iter(TRANSIENT_DELAYS)
        while True:
            try:
                created = create(body)
                break
            except LinearTransient:
                # The write may have landed before Linear failed: adopt it by its marker, or
                # post again after a pause (TRANSIENT_DELAYS), never twice.
                pause = next(pauses, None)
                if pause is None:
                    raise
                SLEEP(pause)
                found = marked()
                if found:
                    created = {"id": found}
                    break
        identity = created.get("id") if isinstance(created, dict) else None
        if not identity:
            raise RuntimeError(f"Linear comment on {issue} returned no ID; reconcile before resuming")
    def matching():
        return [c for c in list_comments(issue) if c.get("id") == identity]
    confirmed = read_back(matching, lambda found: len(found) == 1
                          and (found[0].get("body") or "").strip() == body.strip())
    if len(confirmed) != 1 or (confirmed[0].get("body") or "").strip() != body.strip():
        raise RuntimeError(f"Linear comment read-back failed on {issue}")
    return identity
