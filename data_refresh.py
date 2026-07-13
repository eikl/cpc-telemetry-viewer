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
from datetime import datetime
from pathlib import Path

PULL_SCRIPT = Path(__file__).resolve().parent / "pull_telemetry.sh"
DEFAULT_INTERVAL_SECONDS = 60

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop_event = threading.Event()

_status_lock = threading.Lock()
_status: dict = {"last_run": None, "ok": None, "message": "not run yet"}


def get_status() -> dict:
    with _status_lock:
        return dict(_status)


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


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
    while not _stop_event.is_set():
        _run_once()
        _stop_event.wait(interval)  # wakes immediately on stop, unlike sleep()


def start_background_refresh(interval: int = DEFAULT_INTERVAL_SECONDS) -> None:
    """Idempotently (re)start the pull loop -- a no-op if one is already
    running, but safe to call again after `stop_background_refresh()` to
    resume. Safe to call from every session."""
    global _thread
    with _lock:
        if is_running():
            return
        _stop_event.clear()
        _thread = threading.Thread(target=_loop, args=(interval,), daemon=True)
        _thread.start()


def stop_background_refresh() -> None:
    """Signal the pull loop to stop after its current run. Idempotent."""
    _stop_event.set()
