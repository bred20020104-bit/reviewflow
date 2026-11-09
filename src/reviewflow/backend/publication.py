"""Separate exact-content commit/push approvals. No merge, force, stash or remote editing."""

import re

from reviewflow.backend.config import https_url
from reviewflow.backend.repair import RepairError
from reviewflow.backend.workspace import _git


class Publisher:
    def __init__(self, workspace, validation):
        self.ws = workspace
        self.validation = validation

    def commit_intent(self, receipt, name, email):
        self.validation.verify(self.ws, receipt)
        if not re.fullmatch(r"[A-Za-z0-9 ._-]{1,80}", name) or not re.fullmatch(
            r"[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+", email
        ):
            raise RepairError("Explicit valid author identity required")
        if self.ws._committed:
            raise RepairError("Workspace already committed")
        return {
            "action": "commit",
            "workspace": str(self.ws.path),
            "base": self.ws.base,
            "branch": self.ws.branch,
            "content": self.ws.current_digest(),
            "author_name": name,
            "author_email": email,
            "message": "ReviewFlow verified repair\n\n"
            "Co-Authored-By: Claude Code <noreply@anthropic.com>\n",
        }

    def commit(self, receipt, name, email, gate, grant):
        intent = self.commit_intent(receipt, name, email)
        ws = self.ws
        if _git(ws.path, ["symbolic-ref", "HEAD"]).decode().strip() != "refs/heads/" + ws.branch:
            raise RepairError("Workspace branch changed")
        base_tree = _git(ws.path, ["rev-parse", ws.base + "^{tree}"]).strip()
        if _git(ws.path, ["write-tree"]).strip() != base_tree:
            raise RepairError("Workspace index changed")
        gate.consume(grant, intent)
        modes = {}
        for entry in _git(ws.path, ["ls-tree", "-r", "-z", ws.base]).split(b"\0"):
            if entry:
                metadata, path = entry.split(b"\t", 1)
                modes[path.decode()] = metadata.split()[0].decode()
        for path, data in ws._expected.items():
            if data == ws._baseline[path]:
                continue
            oid = _git(ws.path, ["hash-object", "-w", "--stdin"], data=data).decode().strip()
            _git(ws.path, ["update-index", "--cacheinfo", modes[path], oid, path])
        tree = _git(ws.path, ["write-tree"]).decode().strip()
        commit = (
            _git(
                ws.path,
                [
                    "-c",
                    "user.name=" + name,
                    "-c",
                    "user.email=" + email,
                    "commit-tree",
                    tree,
                    "-p",
                    ws.base,
                ],
                data=intent["message"].encode(),
            )
            .decode()
            .strip()
        )
        # Compare-and-swap refuses a branch advanced by anyone else.
        _git(ws.path, ["update-ref", "refs/heads/" + ws.branch, commit, ws.base])
        ws._committed = commit
        ws._previous = None
        return commit

    def push_intent(self, endpoint):
        ws = self.ws
        ws.current_digest()
        try:
            endpoint = https_url(endpoint)
        except ValueError:
            raise RepairError("Push requires an explicit credential-free HTTPS URL") from None
        if not ws._committed or not re.fullmatch(r"reviewfix/[a-z0-9][a-z0-9-]{0,47}", ws.branch):
            raise RepairError("Only an approved repair commit/branch can be pushed")
        return {
            "action": "push",
            "endpoint": endpoint,
            "commit": ws._committed,
            "branch": ws.branch,
            "workspace": str(ws.path),
            "history": "commit and all reachable ancestors",
            "credential_helper": "disabled",
        }

    @staticmethod
    def push_command(intent):
        # Explicit URL + restrictive protocols; no remote-name resolution or force flag.
        return [
            "-c",
            "credential.helper=",
            "-c",
            "core.askPass=",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.https.allow=always",
            "-c",
            "http.followRedirects=false",
            "-c",
            "http.sslVerify=true",
            "push",
            "--no-verify",
            "--recurse-submodules=no",
            "--",
            intent["endpoint"],
            intent["commit"] + ":refs/heads/" + intent["branch"],
        ]

    def push(self, endpoint, gate, grant):
        intent = self.push_intent(endpoint)
        # Includes/rewrite/proxy/executable overrides cannot silently redirect approval.
        # Effective config includes config.worktree; --local alone misses URL rewrites there.
        config = _git(self.ws.path, ["config", "--null", "--list", "--includes"])
        for item in config.decode("utf-8").split("\0"):
            key = item.split("\n", 1)[0].lower()
            if key.startswith(
                (
                    "url.",
                    "include.",
                    "includeif.",
                    "http.",
                    "https.",
                    "credential.",
                    "core.sshcommand",
                    "core.gitproxy",
                )
            ) or key.endswith((".receivepack", ".pushurl", ".vcs")):
                raise RepairError("Unsafe local transport configuration; push blocked")
        gate.consume(grant, intent)
        _git(self.ws.path, self.push_command(intent), timeout=60)
        return {"status": "pushed", **intent}
