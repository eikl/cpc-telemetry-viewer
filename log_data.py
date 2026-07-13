"""Remote log discovery and tailing for the CPC host, over SSH.

Log files on the remote (one current `<Service>.log` plus daily rotations
`<Service>.log.YYYY-MM-DD`, tens of MB each) are never synced to disk locally
-- unlike the telemetry CSVs, they're too large and rarely need full-history
access. Instead each view fetches just the tail (optionally pre-filtered by
a remote `grep`) over SSH, on demand.
"""
from __future__ import annotations

import html
import re
import shlex
import subprocess

REMOTE_HOST = "cpc.remote"
REMOTE_USER = "omar"
REMOTE_LOGS_DIR = "/home/omar/aq/omarcpc/local/logs"

# Reuses one multiplexed connection across calls instead of renegotiating SSH
# for every fetch, which is the difference between a snappy live-tail and a
# sluggish one.
_SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=/tmp/cpc-log-viewer-ssh-%C",
    "-o", "ControlPersist=60s",
]

LOG_FILENAME_RE = re.compile(r"^([A-Za-z]+)\.log(?:\.(\d{4}-\d{2}-\d{2}))?$")
LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")
LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _ssh(args: list[str], timeout: int) -> str:
    result = subprocess.run(
        ["ssh", *_SSH_OPTS, f"{REMOTE_USER}@{REMOTE_HOST}", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode not in (0, 1):  # grep exits 1 on "no matches", not an error here
        raise RuntimeError(result.stderr.strip() or f"ssh exited {result.returncode}")
    return result.stdout


def list_log_files() -> dict[str, list[tuple[str, str]]]:
    """service -> [(label, remote_path), ...], newest first."""
    out = _ssh(["ls", "-1", REMOTE_LOGS_DIR], timeout=20)

    by_service: dict[str, list[tuple[str, str, str]]] = {}
    for name in out.splitlines():
        match = LOG_FILENAME_RE.match(name.strip())
        if not match:
            continue
        service, date = match.groups()
        label = "Today (live)" if date is None else date
        sort_key = date or "9999-99-99"
        by_service.setdefault(service, []).append((label, f"{REMOTE_LOGS_DIR}/{name}", sort_key))

    return {
        service: [(label, path) for label, path, _ in sorted(entries, key=lambda t: t[2], reverse=True)]
        for service, entries in by_service.items()
    }


def fetch_lines(remote_path: str, max_lines: int, search: str = "") -> str:
    """The last `max_lines` of `remote_path`, or the last `max_lines` matches
    of `search` (case-insensitive) if given -- filtered remotely so a search
    stays fast even on a 60MB+ file instead of pulling the whole thing over."""
    if search.strip():
        # ssh re-joins trailing argv with spaces and hands the result to the
        # remote shell for its *own* parse, so the pipeline has to arrive as
        # one already-quoted token -- passing ["bash", "-c", cmd] as separate
        # argv elements loses that quoting once ssh flattens them back out.
        pipeline = f"grep -i -- {shlex.quote(search)} {shlex.quote(remote_path)} | tail -n {int(max_lines)}"
        remote_cmd = f"bash -c {shlex.quote(pipeline)}"
        return _ssh([remote_cmd], timeout=30)
    return _ssh(["tail", "-n", str(int(max_lines)), remote_path], timeout=20)


# Injected once into the page (see build_app's FastListTemplate(raw_css=...))
# rather than re-embedded in every live-tail update -- a <style> tag re-added
# on every refresh forces the browser to redo cascade/layout work each time,
# which is what produced the visible flash during live tail.
LOG_VIEWER_CSS = """
.log-viewer {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  background: #fcfcfb;
  color: #0b0b0b;
  border: 1px solid rgba(11,11,11,0.10);
  border-radius: 6px;
  padding: 8px 12px;
  height: 560px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
@media (prefers-color-scheme: dark) {
  .log-viewer {
    background: #1a1a19;
    color: #ffffff;
    border-color: rgba(255,255,255,0.10);
  }
}
.log-viewer .log-line { padding: 1px 0; }
.log-viewer .lvl-DEBUG { color: #898781; }
.log-viewer .lvl-WARNING { color: #ad7900; font-weight: 600; }
.log-viewer .lvl-ERROR { color: #c1512e; font-weight: 600; }
.log-viewer .lvl-CRITICAL { color: #ffffff; background: #d03b3b; font-weight: 700; }
@media (prefers-color-scheme: dark) {
  .log-viewer .lvl-WARNING { color: #fab219; }
  .log-viewer .lvl-ERROR { color: #ec835a; }
  .log-viewer .lvl-CRITICAL { background: #e66767; color: #1a1a19; }
}
.log-viewer .log-empty, .log-viewer .log-message { color: #898781; font-style: italic; }
"""


def render_log_html(text: str, levels: list[str]) -> str:
    """Raw log text -> a scrollable, level-colored HTML block, newest line first."""
    rows = []
    for line in reversed(text.splitlines()):
        if not line.strip():
            continue
        level_match = LEVEL_RE.search(line)
        level = level_match.group(1) if level_match else None
        if level and level not in levels:
            continue
        css_class = f"lvl-{level}" if level else "lvl-OTHER"
        rows.append(f'<div class="log-line {css_class}">{html.escape(line)}</div>')

    body = "\n".join(rows) if rows else '<div class="log-empty">No matching lines.</div>'
    return f'<div class="log-viewer">\n{body}\n</div>'


def render_message_html(message: str) -> str:
    """A one-line status/error message, styled to match the log viewer."""
    return f'<div class="log-viewer"><div class="log-message">{html.escape(message)}</div></div>'
