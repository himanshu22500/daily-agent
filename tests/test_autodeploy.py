"""Deploy on merge (issue #128). Offline: a fake machine (git, the deploy
scripts, the feed's /version) and a fake GitHub API."""

from __future__ import annotations

import fcntl
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from daily_agent.autodeploy import (
    KEEP_BACKUPS,
    MAC,
    SERVER,
    AutoDeploy,
    DeployError,
    ci_verdict,
    pg_dump_args,
    repo_slug,
)

OLD, NEW, NEWER = "a" * 40, "b" * 40, "c" * 40
DB_URL = "postgresql+psycopg://daily_agent:s3cret@localhost:5432/daily_agent"
T0 = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


def run_of(
    name: str, conclusion: str | None = "success", started="2026-11-14T09:00:00Z"
):
    return {
        "id": 1,
        "name": name,
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        "started_at": started,
    }


GREEN = {"check_runs": [run_of("tests"), run_of("tests (postgres)")]}


class Machine:
    """A fake checkout plus the feed running from it."""

    def __init__(self, tmp_path: Path):
        self.head = OLD
        self.origin = OLD
        self.dirty = ""
        self.running = OLD  # what the local feed's /version reports
        self.server_running = OLD  # the feed server's, as the Mac sees it
        self.broken: set[str] = set()  # commits whose feed never comes up
        self.backup_fails = False
        self.checks: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.tmp = tmp_path

    def run(self, args, *, cwd, env, capture_output, text, timeout):
        self.calls.append(list(args))
        self.envs.append(env)
        out, code = "", 0
        if args[:2] == ["git", "rev-parse"]:
            out = self.origin if args[2] == "origin/main" else self.head
        elif args[:2] == ["git", "status"]:
            out = self.dirty
        elif args[:3] == ["git", "remote", "get-url"]:
            out = "git@github.com:me/daily-agent.git"
        elif args[:2] == ["git", "log"]:
            out = f"{args[-1][:7]} “A change”"
        elif args[:2] == ["git", "reset"]:
            self.head = args[-1]
        elif args[:2] == ["bash", "deploy/server/update.sh"]:
            sha = env["DEPLOY_SHA"]
            if sha in self.broken:
                code = 1
            else:
                self.running = sha
        elif args[:2] == ["bash", "-c"]:  # the backup
            if self.backup_fails:
                code = 1
            else:
                Path(env["DEPLOY_BACKUP"]).write_bytes(b"dump")
        elif args[0] == "launchctl":
            if self.head not in self.broken:
                self.running = self.head
        return subprocess.CompletedProcess(args, code, stdout=out, stderr="boom" * code)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            sha = request.url.path.split("/")[-2]
            assert request.url.path == f"/repos/me/daily-agent/commits/{sha}/check-runs"
            return httpx.Response(200, json=self.checks.get(sha, {"check_runs": []}))
        if request.url.path == "/version":
            local = request.url.host == "127.0.0.1"
            return httpx.Response(
                200, json={"commit": self.running if local else self.server_running}
            )
        return httpx.Response(404)

    def ran(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


@pytest.fixture
def machine(tmp_path) -> Machine:
    return Machine(tmp_path)


def deployer(machine: Machine, role=SERVER, said=None, **kw) -> AutoDeploy:
    return AutoDeploy(
        role,
        machine.tmp / "app",
        state_dir=machine.tmp / "state",
        http=httpx.Client(transport=httpx.MockTransport(machine.handler)),
        notify=(said.append if said is not None else None),
        run=machine.run,
        server_url="http://daily-agent.example",
        database_url=DB_URL,
        sleep=lambda s: None,
        clock=lambda: T0,
        health_seconds=0,
        **kw,
    )


def test_nothing_new_does_nothing(machine):
    assert deployer(machine).once() == "up-to-date"
    assert not machine.ran("bash")


def test_waits_for_both_ci_checks(machine):
    machine.origin = NEW
    d = deployer(machine)
    assert d.once() == "waiting-for-ci"  # no runs yet
    machine.checks[NEW] = {
        "check_runs": [run_of("tests"), run_of("tests (postgres)", None)]
    }
    assert d.once() == "waiting-for-ci"
    assert machine.head == OLD and not machine.ran("bash")


def test_server_backs_up_then_deploys_the_exact_commit(machine):
    machine.origin = NEW
    machine.checks[NEW] = GREEN
    said: list[str] = []
    assert deployer(machine, said=said).once() == "deployed"
    assert machine.head == NEW and machine.running == NEW
    backup, update = (
        machine.ran("bash", "-c")[0],
        machine.ran("bash", "deploy/server/update.sh"),
    )
    assert machine.calls.index(backup) < machine.calls.index(update[0])
    assert machine.envs[machine.calls.index(update[0])]["DEPLOY_SHA"] == NEW
    # pg_dump gets the password from the environment, never the command line.
    assert "s3cret" not in " ".join(backup)
    assert machine.envs[machine.calls.index(backup)]["PGPASSWORD"] == "s3cret"
    dumps = list((machine.tmp / "state" / "backups").glob("daily_agent-*.sql.gz"))
    assert [d.name for d in dumps] == ["daily_agent-20261114-090000-aaaaaaa.sql.gz"]
    assert said == ["Deployed bbbbbbb “A change” to the feed server."]
    assert deployer(machine).once() == "up-to-date"


def test_only_the_last_backups_are_kept(machine):
    backups = machine.tmp / "state" / "backups"
    backups.mkdir(parents=True)
    for n in range(KEEP_BACKUPS + 3):
        (backups / f"daily_agent-20260101-0000{n:02d}-aaaaaaa.sql.gz").write_bytes(b"")
    machine.origin = NEW
    machine.checks[NEW] = GREEN
    deployer(machine).once()
    kept = sorted(p.name for p in backups.glob("*.sql.gz"))
    assert len(kept) == KEEP_BACKUPS
    assert kept[-1] == "daily_agent-20261114-090000-aaaaaaa.sql.gz"


def test_a_failed_ci_is_never_deployed_and_said_once(machine):
    machine.origin = NEW
    machine.checks[NEW] = {
        "check_runs": [run_of("tests"), run_of("tests (postgres)", "failure")]
    }
    said: list[str] = []
    d = deployer(machine, said=said)
    assert d.once() == "ci-failed"
    assert d.once() == "known-bad"
    assert said == [
        "Not deploying bbbbbbb “A change” to the feed server: "
        "CI failed: tests (postgres) failure."
    ]
    assert machine.head == OLD
    # A fixed commit after it deploys as usual.
    machine.origin = NEWER
    machine.checks[NEWER] = GREEN
    assert d.once() == "deployed" and machine.running == NEWER


def test_a_feed_that_doesnt_come_up_is_rolled_back(machine):
    machine.origin = NEW
    machine.checks[NEW] = GREEN
    machine.broken = {NEW}
    said: list[str] = []
    d = deployer(machine, said=said)
    assert d.once() == "rolled-back"
    assert machine.head == OLD and machine.running == OLD
    [message] = said
    assert message.startswith("Deploying bbbbbbb “A change” to the feed server failed")
    assert "Rolled back to aaaaaaa; that commit won't be retried." in message
    assert "The database was backed up first: " in message
    assert d.once() == "known-bad"
    assert "update.sh exited 1" in d.state.bad[NEW]


def test_a_failed_rollback_is_loud(machine):
    machine.origin = NEW
    machine.checks[NEW] = GREEN
    machine.broken = {NEW, OLD}
    said: list[str] = []
    assert deployer(machine, said=said).once() == "rolled-back"
    assert said[0].startswith("⚠️ Deploying bbbbbbb")
    assert "The feed there may be down." in said[0]


def test_no_deploy_without_a_backup(machine):
    machine.origin = NEW
    machine.checks[NEW] = GREEN
    machine.backup_fails = True
    said: list[str] = []
    d = deployer(machine, said=said)
    assert d.once() == "backup-failed"
    assert d.once() == "backup-failed"
    assert len(said) == 1 and "the database backup failed" in said[0]
    assert machine.head == OLD and not machine.ran("bash", "deploy/server/update.sh")
    assert not list((machine.tmp / "state" / "backups").glob("*.gz"))  # no partial
    machine.backup_fails = False
    assert d.once() == "deployed"


def test_local_edits_are_left_alone(machine):
    machine.origin = NEW
    machine.checks[NEW] = GREEN
    machine.dirty = " M src/daily_agent/cli.py"
    said: list[str] = []
    d = deployer(machine, said=said)
    assert d.once() == "local-edits" and d.once() == "local-edits"
    assert len(said) == 1 and "has local edits" in said[0]
    assert machine.head == OLD


def test_the_mac_waits_for_the_server_then_restarts_its_feed(machine):
    machine.origin = NEW
    machine.checks[NEW] = GREEN
    said: list[str] = []
    d = deployer(machine, role=MAC, said=said, uv="/opt/uv")
    assert d.once() == "waiting-for-server"
    assert machine.head == OLD
    machine.server_running = NEW
    assert d.once() == "deployed"
    assert machine.ran("/opt/uv", "sync", "-q", "--frozen")
    [kick] = machine.ran("launchctl", "kickstart", "-k")
    assert kick[-1].endswith("/com.daily-agent.web")
    assert not machine.ran("bash")  # no backup, no server script
    assert said == ["Deployed bbbbbbb “A change” to the Mac."]


def test_the_mac_needs_the_servers_url(machine):
    with pytest.raises(ValueError):
        AutoDeploy(MAC, machine.tmp, state_dir=machine.tmp, http=httpx.Client())
    with pytest.raises(ValueError):
        AutoDeploy("laptop", machine.tmp, state_dir=machine.tmp, http=httpx.Client())


def test_paused_does_nothing_until_resumed(machine):
    machine.origin = NEW
    machine.checks[NEW] = GREEN
    d = deployer(machine)
    d.pause()
    assert d.once() == "paused" and machine.calls == []
    d.resume()
    assert d.once() == "deployed"


def test_one_pass_at_a_time(machine):
    d = deployer(machine)
    (machine.tmp / "state").mkdir()
    with open(machine.tmp / "state" / "autodeploy.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert d.once() == "busy"
    assert d.once() == "up-to-date"


def test_telegram_trouble_never_stops_a_deploy(machine, capsys):
    machine.origin = NEW
    machine.checks[NEW] = GREEN

    def broken(text):
        raise RuntimeError("telegram down")

    d = deployer(machine)
    d.notify = broken
    assert d.once() == "deployed"
    assert "Telegram failed: telegram down" in capsys.readouterr().out


def test_github_trouble_means_wait(machine):
    machine.origin = NEW

    def down(request):
        if request.url.host == "api.github.com":
            return httpx.Response(403, json={"message": "rate limited"})
        return machine.handler(request)

    d = deployer(machine)
    d.http = httpx.Client(transport=httpx.MockTransport(down))
    assert d.once() == "waiting-for-ci"


def test_ci_verdict():
    assert ci_verdict(GREEN) == "success"
    assert ci_verdict({}) == "pending"
    rerun = {
        "check_runs": [
            run_of("tests", "failure", started="2026-11-14T09:00:00Z"),
            run_of("tests", "success", started="2026-11-14T09:05:00Z"),
            run_of("tests (postgres)"),
        ]
    }
    assert ci_verdict(rerun) == "success"  # the latest run of each check counts
    both = {
        "check_runs": [
            run_of("tests", "cancelled"),
            run_of("tests (postgres)", "failure"),
        ]
    }
    assert ci_verdict(both) == "failed: tests cancelled, tests (postgres) failure"


def test_repo_slug():
    assert repo_slug("https://github.com/me/daily-agent.git") == "me/daily-agent"
    assert repo_slug("git@github.com:me/daily-agent.git\n") == "me/daily-agent"
    assert repo_slug("https://github.com/me/daily-agent") == "me/daily-agent"
    with pytest.raises(DeployError):
        repo_slug("https://gitlab.com/me/daily-agent.git")


def test_pg_dump_args():
    args, env = pg_dump_args(DB_URL)
    assert args == [
        "pg_dump",
        "--no-owner",
        "--dbname",
        "daily_agent",
        "--host",
        "localhost",
        "--port",
        "5432",
        "--username",
        "daily_agent",
    ]
    assert env == {"PGPASSWORD": "s3cret"}
    with pytest.raises(DeployError):
        pg_dump_args("sqlite:///x.db")


def test_a_green_commit_is_asked_about_once(machine):
    machine.origin = NEW
    machine.checks[NEW] = GREEN
    asked = []

    def counting(request):
        if request.url.host == "api.github.com":
            asked.append(request.url.path)
        return machine.handler(request)

    d = deployer(machine, role=MAC)
    d.http = httpx.Client(transport=httpx.MockTransport(counting))
    for _ in range(3):
        assert d.once() == "waiting-for-server"
    assert len(asked) == 1
