# Always-on feed server (shared Postgres)

The feed is served from a small Lightsail server so the phone can read it while
the Mac is asleep (issue #111; decision in `docs/decisions.md`, 2026-11-12).

```
Mac (when awake)                           Lightsail "daily-agent" (always on)
transcripts -> claude -p -> insights ─┐    Postgres 16 (database daily_agent)
YouTube/Instagram -> claude -p -> stories ├──▶   ▲
Telegram delivery                     ┘    │    daily-agent web  ◀── phone
                                                 (tailscale serve, port 80)
```

Both machines read and write the same Postgres database directly. Everything is
reachable only over the tailnet; the Lightsail firewall has no public ports.

| | Where |
|---|---|
| Server | Lightsail `daily-agent`, `ap-south-1a`, bundle `micro_3_1` (1 GB, $7/mo), Ubuntu 24.04 |
| AWS account | the maintainer's personal account (CLI profile `himanshu`) |
| Feed URL (phone) | `http://daily-agent/` on the tailnet |
| Database URL | `postgresql://daily_agent:<password>@<server tailscale ip>/daily_agent` |
| DB password | `~/.config/daily-agent/db-password` on the Mac, and the server's `~/daily-agent/.env` |
| SSH | `ssh -i ~/.ssh/daily-agent-lightsail ubuntu@daily-agent` (over the tailnet) |
| Backups | Lightsail automatic snapshots, daily |

## Day to day

Deploy a new version after merging to `main`:

```bash
ssh -i ~/.ssh/daily-agent-lightsail ubuntu@daily-agent 'bash ~/daily-agent/deploy/server/update.sh'
```

Check the server and the data:

```bash
ssh -i ~/.ssh/daily-agent-lightsail ubuntu@daily-agent 'systemctl status daily-agent-web --no-pager; journalctl -u daily-agent-web -n 30 --no-pager'
uv run daily-agent db info     # on the Mac, in the live install: which DB and row counts
```

The Mac keeps running its launchd jobs unchanged (sweep, SessionEnd hook, and
`com.daily-agent.web`, which also writes the YouTube/Instagram stories). Its
`.env` has `DAILY_AGENT_DATABASE_URL`, so they all write to the server. When
the Mac is offline their writes fail and are retried on the next run; transcript
cursors only advance after a successful write.

## Setting it up from scratch

1. Create the server (personal AWS profile):

   ```bash
   export AWS_PROFILE=himanshu AWS_REGION=ap-south-1
   ssh-keygen -t ed25519 -N "" -f ~/.ssh/daily-agent-lightsail
   aws lightsail import-key-pair --key-pair-name daily-agent-ssh \
     --public-key-base64 "$(cat ~/.ssh/daily-agent-lightsail.pub)"
   aws lightsail create-instances --instance-names daily-agent \
     --availability-zone ap-south-1a --blueprint-id ubuntu_24_04 \
     --bundle-id micro_3_1 --key-pair-name daily-agent-ssh
   aws lightsail enable-add-on --resource-name daily-agent \
     --add-on-request 'addOnType=AutoSnapshot,autoSnapshotAddOnRequest={snapshotTimeOfDay=21:30}'
   ```

2. Install Postgres, Tailscale and uv, and create the databases (with the `C`
   collation, which the stores rely on):

   ```bash
   (umask 077; openssl rand -hex 24 > ~/.config/daily-agent/db-password)
   scp -i ~/.ssh/daily-agent-lightsail deploy/server/bootstrap.sh ubuntu@<public ip>:/tmp/
   ssh -i ~/.ssh/daily-agent-lightsail ubuntu@<public ip> \
     "DB_PASSWORD=$(cat ~/.config/daily-agent/db-password) bash /tmp/bootstrap.sh"
   ssh -i ~/.ssh/daily-agent-lightsail ubuntu@<public ip> 'sudo tailscale up --hostname daily-agent'
   ```

   Open the printed link to add the server to the tailnet.

3. Close every public port; from now on SSH goes over the tailnet:

   ```bash
   aws lightsail put-instance-public-ports --instance-name daily-agent --port-infos '[]'
   ```

4. Write the server's `~/daily-agent/.env`:

   ```bash
   DAILY_AGENT_DATABASE_URL=postgresql://daily_agent:<password>@localhost/daily_agent
   DAILY_AGENT_WEB_VIDEO_SYNC=false
   DAILY_AGENT_WEB_ALLOWED_HOSTS=daily-agent,daily-agent.<tailnet>.ts.net,<server tailscale ip>
   # Only if Instagram is connected: the Sources page looks accounts up with it.
   DAILY_AGENT_INSTAGRAM_TOKEN=...
   DAILY_AGENT_INSTAGRAM_USER_ID=...
   ```

   Then run `deploy/server/update.sh` there.

5. Move the live data once. Stop the Mac's jobs so nothing writes mid-copy, copy
   a snapshot, and point the Mac at the server:

   ```bash
   cd ~/workspace/personal/daily-agent
   launchctl bootout gui/$(id -u)/com.daily-agent.insights-sweep
   launchctl bootout gui/$(id -u)/com.daily-agent.web
   cp daily_agent.db /tmp/daily_agent-$(date +%Y%m%d-%H%M).db
   uv run daily-agent db copy /tmp/daily_agent-<stamp>.db \
     --to "postgresql://daily_agent:$(cat ~/.config/daily-agent/db-password)@<server tailscale ip>/daily_agent"
   echo "DAILY_AGENT_DATABASE_URL=postgresql://daily_agent:<password>@<server tailscale ip>/daily_agent" >> .env
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.daily-agent.insights-sweep.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.daily-agent.web.plist
   uv run daily-agent db info
   ```

   The old `daily_agent.db` stays in place. To go back to it, remove
   `DAILY_AGENT_DATABASE_URL` from `.env` and restart the jobs.

## Restoring

Lightsail keeps the last 7 daily snapshots. Restore one into a new instance from
the console or with `aws lightsail create-instances-from-snapshot`, add it to the
tailnet, then update the IP in the Mac's `DAILY_AGENT_DATABASE_URL`. For a
portable copy at any time:

```bash
ssh -i ~/.ssh/daily-agent-lightsail ubuntu@daily-agent 'sudo -u postgres pg_dump -Fc daily_agent' > daily_agent.dump
```
