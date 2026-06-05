"""Huly (task tracking) source.

Huly has no Python SDK and no plain REST resource API — data is only practically
readable through the official TypeScript SDK against its typed document model.
So we keep a tiny Node bridge (``bridges/huly/index.js``) that talks to Huly and
emits JSON, and this client shells out to it.

Run ``yarn install`` once inside ``bridges/huly/`` to install the bridge's deps.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from ..cache import Cache


class HulyError(RuntimeError):
    pass


class HulyNotConfigured(RuntimeError):
    pass


def _default_bridge() -> Path:
    # src/daily_agent/sources/huly.py -> repo root is parents[3]
    return Path(__file__).resolve().parents[3] / "bridges" / "huly" / "index.js"


class HulyClient:
    def __init__(
        self,
        *,
        workspace: str,
        url: str = "https://huly.app",
        email: str = "",
        password: str = "",
        token: str = "",
        node_bin: str = "node",
        bridge_path: Path | None = None,
        cache: Cache | None = None,
        cache_ttl: int = 600,
    ) -> None:
        if not workspace or not (token or (email and password)):
            raise HulyNotConfigured(
                "Huly is not configured. Set DAILY_AGENT_HULY_WORKSPACE and either "
                "DAILY_AGENT_HULY_TOKEN or DAILY_AGENT_HULY_EMAIL + DAILY_AGENT_HULY_PASSWORD."
            )
        self.node_bin = node_bin
        self.bridge_path = bridge_path or _default_bridge()
        self._cache = cache
        self._ttl = cache_ttl
        self._env = {
            **os.environ,
            "HULY_URL": url,
            "HULY_WORKSPACE": workspace,
            "HULY_EMAIL": email,
            "HULY_PASSWORD": password,
            "HULY_TOKEN": token,
        }

    async def __aenter__(self) -> "HulyClient":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def _run(self, *args: str) -> object:
        if not self.bridge_path.exists():
            raise HulyError(f"Huly bridge not found at {self.bridge_path}")
        if not (self.bridge_path.parent / "node_modules").exists():
            raise HulyError(
                f"Huly bridge deps not installed. Run: (cd {self.bridge_path.parent} && yarn install)"
            )
        proc = await asyncio.create_subprocess_exec(
            self.node_bin, str(self.bridge_path), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            msg = err.decode().strip().splitlines()[-1] if err else "unknown error"
            raise HulyError(f"huly bridge failed: {msg}")
        try:
            return json.loads(out.decode())
        except json.JSONDecodeError as e:
            raise HulyError(f"huly bridge returned invalid JSON: {e}")

    # --- queries ---------------------------------------------------------- #
    async def projects(self) -> list[dict]:
        """List Huly projects: [{identifier, name, description}]."""
        if self._cache and (hit := self._cache.get("huly:projects", self._ttl)) is not None:
            return hit
        result = await self._run("projects")
        if self._cache:
            self._cache.set("huly:projects", result)
        return result  # type: ignore[return-value]

    async def issues(
        self,
        project: str | None = None,
        *,
        limit: int = 50,
        status: str | None = None,
        assignee: str | None = None,
        priority: str | None = None,
    ) -> list[dict]:
        """List issues, most-recently-modified first.

        Optional filters: ``project`` identifier, ``status`` name, ``assignee``
        name (substring match), ``priority`` (none|urgent|high|medium|low).
        """
        args = ["issues", "--limit", str(limit)]
        if project:
            args += ["--project", project]
        if status:
            args += ["--status", status]
        if assignee:
            args += ["--assignee", assignee]
        if priority:
            args += ["--priority", priority]
        # Lists can gain/lose members -> always TTL (no permanence).
        key = "huly:issues:" + "|".join(args[1:])
        if self._cache and (hit := self._cache.get(key, self._ttl)) is not None:
            return hit
        result = await self._run(*args)
        if self._cache:
            self._cache.set(key, result)
        return result  # type: ignore[return-value]

    async def issue(self, identifier: str) -> dict | None:
        """Fetch one issue's detail (incl. markdown description) by identifier.

        A DONE issue is terminal, so it's cached permanently; otherwise TTL.
        """
        key = f"huly:issue:{identifier.upper()}"
        if self._cache and (hit := self._cache.get(key, self._ttl)) is not None:
            return hit
        result = await self._run("issue", identifier)
        if self._cache and result:
            self._cache.set(key, result, permanent=result.get("statusCategory") == "done")
        return result  # type: ignore[return-value]
