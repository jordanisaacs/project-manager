# pm — systemd user units

Units that keep the canonical-repo store warm, its pinned branches up to
date, and the project integration daemon running.

| File                       | Role                                                                |
| -------------------------- | ------------------------------------------------------------------- |
| `pm-repo-mgmt.service`     | Runs `pm repo pull` + `pm repo maintenance` (fetch, ff-only, gc).    |
| `pm-repo-mgmt.timer`       | Fires the service 15s after boot and every 15 minutes thereafter.    |
| `pm-serve.service`         | Long-running `pm serve` project integration daemon (auto-restart). |

The services use `%h` so they work for any user; they expect `pm` at
`~/.local/bin/pm` — adjust `ExecStart=` if your install lives elsewhere.

## `pm-serve` (project integrations)

`pm serve` is a loopback HTTP daemon exposing versioned project discovery,
durable project leases, and health endpoints for web integrations. Port and
allowed browser origins come from `[serve]` in the pm config; the default port
is `8787`. Cross-origin web clients must be listed in
`[serve].allowed_origins`; loopback development origins are allowed by default.

Agent status tracking is local to the Emacs/Ghostel integration. `pm serve`
does not ingest lifecycle status, poll transcripts, persist agent state, or
provide an event stream. Existing `~/.pm/serve.db` files are left untouched.

```toml
[serve]
allowed_origins = ["https://omnigent.example.com"]
```

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
