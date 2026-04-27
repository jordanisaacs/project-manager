# pm — systemd user units

Two oneshot/timer units that keep the canonical-repo store warm and
its pinned branches up to date.

| File                       | Role                                                                |
| -------------------------- | ------------------------------------------------------------------- |
| `pm-repo-mgmt.service`     | Runs `pm repo pull` + `pm repo maintenance` (fetch, ff-only, gc).    |
| `pm-repo-mgmt.timer`       | Fires the service 15s after boot and every 15 minutes thereafter.    |

The service uses `%h` so it works for any user; it expects `pm` at
`~/.local/bin/pm` — adjust `ExecStart=` if your install lives elsewhere.

## Install

```sh
ln -sf "$PWD"/pm-repo-mgmt.service ~/.config/systemd/user/
ln -sf "$PWD"/pm-repo-mgmt.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pm-repo-mgmt.timer
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
