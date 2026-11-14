"""Deploy on merge (issue #128): each machine updates itself.

A timer runs one pass every couple of minutes, systemd on the feed server and
launchd on the Mac:

1. ``git fetch`` main. Nothing new: done.
2. Wait until both CI checks (``tests``, ``tests (postgres)``) pass for that
   exact commit. A commit whose CI failed is never deployed; Telegram says so
   once.
3. Leave a live checkout with local edits (tracked files) alone; say so once.
4. Mac: wait until the server runs that commit. The database backup and any
   schema change then happen first, and on the server.
5. Server: ``pg_dump`` the database, keeping the last ``KEEP_BACKUPS`` dumps.
6. Deploy: on the server, ``deploy/server/update.sh`` pinned to the commit; on
   the Mac, reset, ``uv sync`` and restart the feed. Then wait for the local
   ``/version`` to report the commit.
7. If it doesn't come up, roll back to the previous commit and remember the bad
   one, which is never retried. Telegram says so. A later commit deploys as
   usual.

GitHub never reaches either machine and holds no keys. The repo is public, so
the CI check is an unauthenticated API read, and code is pulled as before.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy.engine import make_url

SERVER, MAC = "server", "mac"
ROLES = (SERVER, MAC)
REQUIRED_CHECKS = ("tests", "tests (postgres)")
KEEP_BACKUPS = 14
HEALTH_SECONDS = 90.0
HEALTH_POLL_SECONDS = 2.0
LOCAL_URL = "http://127.0.0.1:8765"
MAC_WEB_LABEL = "com.daily-agent.web"
GITHUB_API = "https://api.github.com"
# Outcomes of a pass that change nothing and aren't worth a log line.
QUIET = ("up-to-date", "paused", "busy")
MAX_REMEMBERED = 30


class DeployError(RuntimeError):
    """A step failed; the message says which and why."""


def repo_slug(remote_url: str) -> str:
    """``owner/repo`` from a GitHub remote (HTTPS or SSH)."""
    match = re.search(
        r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", remote_url.strip()
    )
    if not match:
        raise DeployError(f"origin is not a GitHub repo: {remote_url!r}")
    return match.group(1)


def ci_verdict(payload: dict, required: tuple[str, ...] = REQUIRED_CHECKS) -> str:
    """``success``, ``pending``, or ``failed: …`` from a commit's check runs.

    The latest run of each required check counts (a re-run replaces a failure).
    A check that hasn't started yet counts as pending.
    """
    runs = payload.get("check_runs") or []
    failed: list[str] = []
    pending = False
    for name in required:
        mine = [run for run in runs if run.get("name") == name]
        if not mine:
            pending = True
            continue
        latest = max(mine, key=lambda r: (r.get("started_at") or "", r.get("id") or 0))
        if latest.get("status") != "completed":
            pending = True
        elif latest.get("conclusion") != "success":
            failed.append(f"{name} {latest.get('conclusion') or 'failed'}")
    if failed:
        return "failed: " + ", ".join(failed)
    return "pending" if pending else "success"


def pg_dump_args(database_url: str) -> tuple[list[str], dict[str, str]]:
    """``pg_dump`` arguments for a Postgres URL; the password goes in the env,
    never on the command line."""
    url = make_url(database_url)
    if not url.drivername.startswith("postgres"):
        raise DeployError(f"not a Postgres URL: {url.drivername}")
    args = ["pg_dump", "--no-owner", "--dbname", url.database or ""]
    if url.host:
        args += ["--host", url.host]
    if url.port:
        args += ["--port", str(url.port)]
    if url.username:
        args += ["--username", url.username]
    env = {"PGPASSWORD": url.password} if url.password else {}
    return args, env


@dataclass
class State:
    """What a machine remembers between passes (a small JSON file)."""

    path: Path
    bad: dict[str, str] = field(default_factory=dict)  # commit -> why
    told: dict[str, str] = field(default_factory=dict)  # commit -> what was said
    # Commits whose CI passed: not asked again (GitHub allows 60 calls an hour).
    green: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        return cls(
            path=path,
            bad=dict(data.get("bad") or {}),
            told=dict(data.get("told") or {}),
            green=list(data.get("green") or []),
        )

    def save(self) -> None:
        trim = lambda d: dict(list(d.items())[-MAX_REMEMBERED:])  # noqa: E731
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "bad": trim(self.bad),
                    "told": trim(self.told),
                    "green": self.green[-MAX_REMEMBERED:],
                },
                indent=1,
            )
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AutoDeploy:
    """One machine's deploy-on-merge. ``once()`` is one pass."""

    def __init__(
        self,
        role: str,
        app_dir: str | Path,
        *,
        state_dir: str | Path,
        http: httpx.Client,
        notify: Callable[[str], None] | None = None,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        server_url: str = "",
        local_url: str = LOCAL_URL,
        database_url: str = "",
        backup_dir: str | Path | None = None,
        uv: str = "uv",
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _now,
        health_seconds: float = HEALTH_SECONDS,
    ) -> None:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}, not {role!r}")
        if role == MAC and not server_url:
            raise ValueError("the Mac needs the server's URL (DAILY_AGENT_FEED_URL)")
        self.role = role
        self.app_dir = Path(app_dir)
        self.state_dir = Path(state_dir)
        self.http = http
        self.notify = notify
        self.run = run
        self.server_url = server_url.rstrip("/")
        self.local_url = local_url.rstrip("/")
        self.database_url = database_url
        self.backup_dir = Path(backup_dir) if backup_dir else self.state_dir / "backups"
        self.uv = uv
        self.sleep = sleep
        self.clock = clock
        self.health_seconds = health_seconds

    # --- the pause switch ---------------------------------------------------- #
    @property
    def pause_file(self) -> Path:
        return self.state_dir / "autodeploy.paused"

    @property
    def paused(self) -> bool:
        return self.pause_file.exists()

    def pause(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.pause_file.touch()

    def resume(self) -> None:
        self.pause_file.unlink(missing_ok=True)

    @property
    def state(self) -> State:
        return State.load(self.state_dir / "autodeploy.json")

    @property
    def where(self) -> str:
        return "feed server" if self.role == SERVER else "Mac"

    # --- one pass ---------------------------------------------------------------- #
    def once(self) -> str:
        """Deploy main if it's new and green; return what happened (a short code)."""
        if self.paused:
            return "paused"
        with self._lock() as held:
            if not held:
                return "busy"
            return self._pass()

    def _pass(self) -> str:
        self._git("fetch", "-q", "origin", "main")
        target = self._git("rev-parse", "origin/main")
        current = self._git("rev-parse", "HEAD")
        if target == current:
            return "up-to-date"
        state = self.state
        if target in state.bad:
            return "known-bad"
        verdict = "success" if target in state.green else self._ci(target)
        if verdict == "success" and target not in state.green:
            state.green.append(target)
            state.save()
        if verdict == "pending":
            return "waiting-for-ci"
        what = self._describe(target)
        if verdict != "success":
            state.bad[target] = f"CI {verdict}"
            state.save()
            self._tell(f"Not deploying {what} to the {self.where}: CI {verdict}.")
            return "ci-failed"
        if self._git("status", "--porcelain", "--untracked-files=no"):
            if state.told.get(target) != "local-edits":
                state.told[target] = "local-edits"
                state.save()
                self._tell(
                    f"Not deploying {what} to the {self.where}: its checkout "
                    f"({self.app_dir}) has local edits. Commit or discard them."
                )
            return "local-edits"
        if self.role == MAC and self._version(self.server_url) != target:
            return "waiting-for-server"
        backup = ""
        if self.role == SERVER:
            try:
                backup = self._backup(current)
            except DeployError as exc:
                if state.told.get(target) != "backup-failed":
                    state.told[target] = "backup-failed"
                    state.save()
                    self._tell(
                        f"Not deploying {what} to the {self.where}: the database "
                        f"backup failed ({exc}). Retrying every pass."
                    )
                return "backup-failed"
        try:
            self._deploy(target)
            self._wait_for(target)
        except DeployError as exc:
            self._roll_back(current, target, what, str(exc), backup, state)
            return "rolled-back"
        state.told.pop(target, None)
        state.save()
        self._tell(f"Deployed {what} to the {self.where}.")
        return "deployed"

    def _roll_back(
        self, previous: str, target: str, what: str, why: str, backup: str, state: State
    ) -> None:
        state.bad[target] = why[:300]
        state.save()
        saved = f" The database was backed up first: {backup}." if backup else ""
        try:
            self._deploy(previous)
            self._wait_for(previous)
        except DeployError as exc:
            self._tell(
                f"⚠️ Deploying {what} to the {self.where} failed ({why}), and rolling "
                f"back to {previous[:7]} failed too ({exc}). The feed there may be "
                f"down.{saved}"
            )
            return
        self._tell(
            f"Deploying {what} to the {self.where} failed ({why}). Rolled back to "
            f"{previous[:7]}; that commit won't be retried.{saved}"
        )

    # --- steps ----------------------------------------------------------------- #
    def _ci(self, sha: str) -> str:
        slug = repo_slug(self._git("remote", "get-url", "origin"))
        try:
            reply = self.http.get(
                f"{GITHUB_API}/repos/{slug}/commits/{sha}/check-runs",
                params={"per_page": 100},
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "daily-agent-autodeploy",
                },
            )
        except httpx.HTTPError:
            return "pending"  # offline or GitHub down: try again next pass
        if reply.status_code != 200:
            return "pending"  # rate limit and the like pass
        try:
            return ci_verdict(reply.json())
        except ValueError:
            return "pending"

    def _backup(self, current: str) -> str:
        """Dump the database before a deploy; return the file ("" without one)."""
        if not self.database_url.startswith("postgres"):
            return ""
        args, env = pg_dump_args(self.database_url)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.clock().strftime("%Y%m%d-%H%M%S")
        out = self.backup_dir / f"daily_agent-{stamp}-{current[:7]}.sql.gz"
        script = 'set -o pipefail; "$@" | gzip > "$DEPLOY_BACKUP"'
        try:
            self._sh(
                ["bash", "-c", script, "pg_dump", *args],
                env={**env, "DEPLOY_BACKUP": str(out)},
            )
        except DeployError:
            out.unlink(missing_ok=True)
            raise
        dumps = sorted(self.backup_dir.glob("daily_agent-*.sql.gz"))
        for old in dumps[:-KEEP_BACKUPS]:
            old.unlink(missing_ok=True)
        return str(out)

    def _deploy(self, sha: str) -> None:
        self._git("reset", "-q", "--hard", sha)
        if self.role == SERVER:
            # The script from the commit itself, pinned to it (never a newer main).
            self._sh(["bash", "deploy/server/update.sh"], env={"DEPLOY_SHA": sha})
            return
        self._sh([self.uv, "sync", "-q", "--frozen"])
        self._sh(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{MAC_WEB_LABEL}"])

    def _wait_for(self, sha: str) -> None:
        deadline = time.monotonic() + self.health_seconds
        running = ""
        while True:
            running = self._version(self.local_url)
            if running == sha:
                return
            if time.monotonic() >= deadline:
                break
            self.sleep(HEALTH_POLL_SECONDS)
        seen = f"it runs {running[:7]}" if running else "it isn't answering"
        raise DeployError(f"the feed didn't come up on {sha[:7]}: {seen}")

    def _version(self, base_url: str) -> str:
        try:
            reply = self.http.get(f"{base_url}/version", timeout=5.0)
            return (
                str(reply.json().get("commit") or "")
                if reply.status_code == 200
                else ""
            )
        except (httpx.HTTPError, ValueError, AttributeError):
            return ""

    def _describe(self, sha: str) -> str:
        try:
            return self._git("log", "-1", "--format=%h “%s”", sha)
        except DeployError:
            return sha[:7]

    def _tell(self, text: str) -> None:
        print(text, flush=True)
        if self.notify is None:
            return
        try:
            self.notify(text)
        except Exception as exc:  # noqa: BLE001 - a lost message never stops a deploy
            print(f"(Telegram failed: {exc})", flush=True)

    # --- plumbing ---------------------------------------------------------------- #
    def _git(self, *args: str) -> str:
        return self._sh(["git", *args])

    def _sh(self, args: list[str], *, env: dict[str, str] | None = None) -> str:
        try:
            done = self.run(
                args,
                cwd=self.app_dir,
                env={**os.environ, **(env or {})},
                capture_output=True,
                text=True,
                timeout=900,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DeployError(f"{args[0]}: {exc}") from exc
        if done.returncode != 0:
            tail = (done.stderr or done.stdout or "").strip().splitlines()[-3:]
            raise DeployError(
                f"{' '.join(args[:3])} exited {done.returncode}: {' / '.join(tail)}"
            )
        return (done.stdout or "").strip()

    @contextmanager
    def _lock(self) -> Iterator[bool]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.state_dir / "autodeploy.lock", "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
