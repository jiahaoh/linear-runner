"""Small authenticated Streamable HTTP MCP client; no model or credential logging."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request


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

    def token(self):
        """Reread credentials so refresh by the owning CLI is visible; never refresh secretly."""
        if self.config.get("token_env"):
            token = os.environ.get(self.config["token_env"])
            if not token:
                raise RuntimeError("Configured Linear bearer-token environment variable is missing")
            return token
        path = Path(self.config["credentials_file"]).expanduser()
        entries = json.loads(path.read_text())
        matches = [v for v in entries.values() if isinstance(v, dict)
                   and v.get("server_name") == "linear" and v.get("server_url") == self.url]
        if len(matches) != 1:
            raise RuntimeError("Expected one Linear OAuth credential for the configured endpoint")
        credential = matches[0]
        expiry = credential.get("expires_at", 0)
        if expiry > 100_000_000_000:  # Codex's file backend records milliseconds.
            expiry /= 1000
        if expiry and expiry <= time.time() + 30:
            raise RuntimeError("Linear OAuth expired; refresh with the owning CLI and resume")
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
            raise RuntimeError(f"Linear HTTP {error.code}; reconcile authentication/write outcome before resume") from None

    def call(self, name, **arguments):
        if not self.initialized:
            result = self.rpc("initialize", {"protocolVersion": self.protocol, "capabilities": {},
                                             "clientInfo": {"name": "linear-codex-runner", "version": "2"}})
            self.protocol = result["protocolVersion"]
            self.rpc("notifications/initialized", notification=True)
            self.initialized = True
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError(f"Linear {name} failed; inspect the issue before retrying")
        texts = [part["text"] for part in result.get("content", []) if part.get("type") == "text"]
        try:
            return json.loads("\n".join(texts))
        except ValueError:
            raise RuntimeError(f"Linear {name} returned unexpected non-JSON content") from None

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
            args = dict(arguments, limit=250)
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
        identity = matches[0].get("id")
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

    def summary(self, issue, marker, body):
        """Reconcile by stable marker after a crash between remote write and local save."""
        matches = [c for c in self.comments(issue) if marker in c.get("body", "")]
        if len(matches) > 1:
            raise RuntimeError("Duplicate execution-summary marker; reconcile manually")
        text = body + "\n\n" + marker
        if matches:
            if matches[0]["body"] != text:
                self.call("save_comment", id=matches[0]["id"], body=text)
            identity = matches[0]["id"]
        else:
            identity = self.call("save_comment", issueId=issue, body=text)["id"]
        confirmed = [c for c in self.comments(issue) if c["id"] == identity]
        if len(confirmed) != 1 or confirmed[0]["body"] != text:
            raise RuntimeError("Linear execution summary read-back failed")
        return identity
