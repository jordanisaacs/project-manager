# pm — systemd user units

Units that keep the canonical-repo store warm, its pinned branches up to
date, and the agent-session tracking daemon running.

| File                       | Role                                                                |
| -------------------------- | ------------------------------------------------------------------- |
| `pm-repo-mgmt.service`     | Runs `pm repo pull` + `pm repo maintenance` (fetch, ff-only, gc).    |
| `pm-repo-mgmt.timer`       | Fires the service 15s after boot and every 15 minutes thereafter.    |
| `pm-serve.service`         | Long-running `pm serve` project/session daemon (auto-restart).|

The services use `%h` so they work for any user; they expect `pm` at
`~/.local/bin/pm` — adjust `ExecStart=` if your install lives elsewhere.

## `pm-serve` (project manager and agent session tracker)

`pm serve` is a loopback HTTP+SSE daemon that tracks live agent
session status. It ingests best-effort lifecycle-hook events
(`POST /api/status`), keeps live state in an ephemeral WAL SQLite store
(recreated on each start), backfills via a transcript-tailing poller, and
pushes updates over SSE (`GET /api/stream`, with `?meta.<KEY>=<val>` /
`?agent=` / `?project=` / `?status=` server-side filters). It also exposes
versioned project discovery and durable lease endpoints for configured web
frontends. Port and paths come from `[serve]` in the pm config (default port
`8787`, db `~/.pm/serve.db`). Cross-origin web clients must be listed in
`[serve].allowed_origins`; loopback development origins are allowed by default.

```toml
[serve]
allowed_origins = ["https://omnigent.example.com"]
```

Install the hooks that feed it (idempotent, user level):

```sh
pm agent install-hooks            # both Claude + Codex
pm agent install-hooks --no-codex # Claude only
```

This writes a fire-and-forget reporter to `~/.pm/hooks/status-reporter.sh`
and registers it in `~/.claude/settings.json` and `~/.codex/hooks.json`
(backing each up to `*.pm-bak`). Hook failures are logged to
`~/.pm/hook-debug.log` and never block the agent. Sessions launched with
`PM_META_*` env vars carry those into the session's `meta` for filtering
(e.g. an Emacs client sets `PM_META_SOURCE=emacs`).

## Install

```sh
ln -sf "$PWD"/pm-repo-mgmt.service ~/.config/systemd/user/
ln -sf "$PWD"/pm-repo-mgmt.timer   ~/.config/systemd/user/
ln -sf "$PWD"/pm-serve.service     ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pm-repo-mgmt.timer
systemctl --user enable --now pm-serve.service
```

Verify:

```sh
systemctl --user list-timers pm-repo-mgmt.timer
journalctl --user -u pm-repo-mgmt.service -n 20
```

## Uninstall

```sh
systemctl --user disable --now pm-repo-mgmt.timer
rm ~/.config/systemd/user/pm-repo-mgmt.{service,timer}
systemctl --user daemon-reload
```
