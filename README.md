# cpc-telemetry-viewer

A [Panel](https://panel.holoviz.org/) web dashboard for CPC telemetry: live
plots of the telemetry CSVs synced from the remote host, plus a log viewer
that tails the remote's log files over SSH on demand.

Works on macOS and Linux (anywhere Python, `rsync`, `ssh`, and `flock` are
available).

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) -- manages the Python version and dependencies (`panel`, `hvplot`, `pandas`), no manual `pip install` needed
- `rsync` and `ssh` (with key-based access to the remote telemetry host already set up)
- `flock` -- ships by default on Linux (`util-linux`); on macOS install it via Homebrew (`brew install util-linux` or similar, wherever your system provides it)

## Configuration

- **Remote host**: edit `REMOTE_HOST` / `REMOTE_USER` / `REMOTE_DIR` at the top of `pull_telemetry.sh`.
- **Local data directory**: defaults to `~/Projects/cpc-data`. Override by setting the `CPC_DATA_DIR` environment variable (used by both `pull_telemetry.sh` and `telemetry_data.py` -- keep them in agreement). Don't point this (or the repo clone itself) at `~/Documents`, `~/Desktop`, or `~/Downloads` -- macOS's TCC sandbox silently denies background agents (launchd/systemd) file access under those folders even though an interactive shell works fine there. This cost real debugging time once; see the background-service sections below.

## Running it

```bash
uv run panel serve app.py --show --autoreload
```

The first run creates `.venv` and installs dependencies automatically (from
`pyproject.toml`/`uv.lock`); later runs reuse it. Opens the dashboard at
`http://127.0.0.1:5006/app`. The background thread that pulls fresh telemetry
starts automatically; use the "Stop pulling" / "Resume pulling" button in the
header to pause it without stopping the server.

## Running it as a background service

Templates for both platforms are in `deploy/`. Run `uv sync` once first so
`.venv` exists -- both templates run `.venv/bin/python3` directly rather than
`uv run`, so the service doesn't depend on `uv` (or network access, for
dependency resolution) being available every time it starts.

### Linux (systemd --user)

```bash
uv sync
mkdir -p ~/.config/systemd/user
cp deploy/cpc-telemetry-viewer.service ~/.config/systemd/user/
# edit WorkingDirectory and ExecStart's path to match your clone's location
systemctl --user daemon-reload
systemctl --user enable --now cpc-telemetry-viewer
```

By default a user service only runs while you have an active login session;
to have it survive logout/reboot like a proper background service, enable
lingering once: `loginctl enable-linger $USER`.

Logs go to the journal: `journalctl --user -u cpc-telemetry-viewer -f`.

Useful commands:

```bash
systemctl --user restart cpc-telemetry-viewer   # e.g. after editing app.py
systemctl --user stop cpc-telemetry-viewer
systemctl --user status cpc-telemetry-viewer
```

### macOS (launchd)

```bash
uv sync
cp deploy/com.example.cpc-telemetry-viewer.plist ~/Library/LaunchAgents/com.example.cpc-telemetry-viewer.plist
# edit WorkingDirectory, StandardOutPath/StandardErrorPath, and ProgramArguments' .venv/bin/python3 path
plutil -lint ~/Library/LaunchAgents/com.example.cpc-telemetry-viewer.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.cpc-telemetry-viewer.plist
```

Logs go to whatever file you set `StandardOutPath`/`StandardErrorPath` to.

Useful commands:

```bash
launchctl kickstart -k gui/$(id -u)/com.example.cpc-telemetry-viewer   # restart, e.g. after editing app.py
launchctl bootout gui/$(id -u)/com.example.cpc-telemetry-viewer        # stop and unload
```

If you change the plist itself (paths, arguments), you need the full
bootout + bootstrap cycle above, not just `kickstart` -- `kickstart` restarts
the already-loaded job definition, so it won't pick up plist edits on its own.
