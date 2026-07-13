"""Runs pull_telemetry.sh on a fixed interval, in a background thread.

`panel serve` re-executes the served app script for every browser session,
but this module is only ever *imported* (Python caches imports process-wide),
so the guard in `start_background_refresh` keeps a single pull loop alive no
matter how many sessions/tabs connect. The pull script also takes its own
flock, so even a second process running this loop would just no-op rather
than double-pull.
"""
from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

PULL_SCRIPT = Path(__file__).resolve().parent / "pull_telemetry.sh"
DEFAULT_INTERVAL_SECONDS = 60

_start_lock = threading.Lock()
_started = False

_status_lock = threading.Lock()
_status: dict = {"last_run": None, "ok": None, "message": "not run yet"}


def get_status() -> dict:
    with _status_lock:
        return dict(_status)


def _run_once() -> None:
    try:
        result = subprocess.run(
            [str(PULL_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            ok = False
            message = f"pull failed (exit {result.returncode}): {result.stderr.strip()[-300:]}"
        elif "already running" in result.stdout:
            # The script exits 0 both on a real pull and when it skips
            # because another instance holds the flock -- report the skip
            # distinctly so a stuck/overlapping run is visible immediately
            # instead of looking identical to a healthy pull indefinitely.
            ok = True
            message = "skipped (previous pull still running)"
        else:
            ok = True
            message = "pull succeeded"
    except Exception as exc:
        ok = False
        message = f"pull errored: {exc}"

    with _status_lock:
        _status["last_run"] = datetime.now()
        _status["ok"] = ok
        _status["message"] = message


def _loop(interval: int) -> None:
    while True:
        _run_once()
        time.sleep(interval)


def start_background_refresh(interval: int = DEFAULT_INTERVAL_SECONDS) -> None:
    """Idempotently start the pull loop. Safe to call from every session."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, args=(interval,), daemon=True).start()
