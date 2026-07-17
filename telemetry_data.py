"""Discovery, caching, and loading helpers for CPC telemetry CSVs in `data/`."""
from __future__ import annotations

import io
import os
import re
from datetime import date, datetime, time
from pathlib import Path
from typing import NamedTuple

import pandas as pd

# The data directory lives outside this repo (it's pulled telemetry, not
# code) and isn't tied to wherever the repo happens to be cloned/moved to --
# override with CPC_DATA_DIR if it's not at the default location. Avoid
# ~/Documents, ~/Desktop, ~/Downloads: macOS's TCC sandbox silently denies
# file access under those folders to background agents (launchd/systemd),
# even though an interactive shell in the same location works fine.
DATA_DIR = Path(os.environ.get("CPC_DATA_DIR", "~/Projects/cpc-data")).expanduser()
FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_([A-Za-z]+)_telemetry\.csv$")

MAX_PLOT_POINTS = 5000


class _CachedFile(NamedTuple):
    """A parsed telemetry CSV, plus how many bytes of it we've consumed."""
    mtime: float
    bytes_read: int
    df: pd.DataFrame


# path -> cached parse. Shared process-wide so every session benefits from
# the same cache. A worker's current-day file grows continuously as new
# telemetry rows land, so re-parsing it from scratch on every mtime change
# would get slower over the course of a long-running session; `_load_file`
# instead parses only the bytes appended since the last read (see its
# docstring) and appends them to the cached frame.
_file_cache: dict[Path, _CachedFile] = {}


def discover_files(data_dir: Path = DATA_DIR) -> dict[str, list[Path]]:
    """Map worker name -> sorted list of its daily CSV files."""
    by_worker: dict[str, list[Path]] = {}
    for path in sorted(data_dir.glob("*_telemetry.csv")):
        match = FILENAME_RE.match(path.name)
        if not match:
            continue
        worker = match.group(2)
        by_worker.setdefault(worker, []).append(path)
    return by_worker


def file_date(path: Path) -> date:
    match = FILENAME_RE.match(path.name)
    return datetime.strptime(match.group(1), "%Y-%m-%d").date()


def worker_date_span(files: list[Path]) -> tuple[datetime, datetime]:
    dates = [file_date(p) for p in files]
    return datetime.combine(min(dates), time.min), datetime.combine(max(dates), time.max)


def latest_timestamp(files: list[Path]) -> datetime | None:
    """Most recent `ts` actually present in the data (not just the file's date)."""
    for path in reversed(files):  # files are sorted, so walk back from the latest day
        df = _load_file(path)
        if not df.empty:
            return df["ts"].max().to_pydatetime()
    return None


def latest_value(files: list[Path], column: str) -> float | None:
    """The value of `column` at the most recent timestamp that has one.

    Like `latest_timestamp`, walks back from the newest day's file, since
    only the most recent file(s) can hold the newest row. Rows are
    append-only in time order, so the newest value is almost always the
    column's last entry -- check that first (O(1)) before falling back to a
    full scan for the rare case it's null.
    """
    for path in reversed(files):
        df = _load_file(path)
        if column not in df.columns or df.empty:
            continue
        last = df[column].iloc[-1]
        if pd.notna(last):
            return float(last)
        non_null = df[column].dropna()
        if not non_null.empty:
            return float(non_null.iloc[-1])
    return None


def worker_columns(files: list[Path]) -> list[str]:
    header = pd.read_csv(files[0], nrows=0).columns.tolist()
    return [c for c in header if c != "ts"]


def _clean_ts(df: pd.DataFrame) -> pd.DataFrame:
    # A handful of rows have a blank or malformed `ts` (e.g. two timestamps
    # glued together) from glitches in the live logger; drop rather than crash.
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    return df.dropna(subset=["ts"])


def _load_file(path: Path) -> pd.DataFrame:
    """Parse `path`, reusing the process-wide cache when possible.

    On a cache hit where the file has only grown since the last read, parses
    just the bytes appended since then and concatenates them onto the cached
    frame, instead of re-reading the whole (potentially large, ever-growing)
    file from scratch every time.
    """
    mtime = path.stat().st_mtime
    cached = _file_cache.get(path)
    if cached is not None and cached.mtime == mtime:
        return cached.df

    size = path.stat().st_size
    if cached is None or size < cached.bytes_read:
        # First time seeing this file, or it shrank (truncated/replaced) --
        # (re)parse it from scratch.
        df = _clean_ts(pd.read_csv(path))
        _file_cache[path] = _CachedFile(mtime, size, df)
        return df

    with path.open("rb") as f:
        f.seek(cached.bytes_read)
        new_bytes = f.read()

    # The file can be read mid-write; keep only whole lines so a partial
    # trailing row doesn't get parsed short -- it's picked up complete on a
    # later call, once the writer finishes it (or appends past it).
    if not new_bytes.endswith(b"\n"):
        new_bytes = new_bytes[: new_bytes.rfind(b"\n") + 1]

    if not new_bytes.strip():
        # Nothing new to parse yet, but the mtime moved (e.g. the in-progress
        # line grew) -- remember that without re-touching the dataframe.
        _file_cache[path] = _CachedFile(mtime, cached.bytes_read, cached.df)
        return cached.df

    new_rows = _clean_ts(pd.read_csv(io.BytesIO(new_bytes), header=None, names=list(cached.df.columns)))
    df = pd.concat([cached.df, new_rows], ignore_index=True)
    _file_cache[path] = _CachedFile(mtime, cached.bytes_read + len(new_bytes), df)
    return df


def downsample(series: pd.Series, max_points: int = MAX_PLOT_POINTS) -> pd.Series:
    """Resample long series so the browser never has to render >max_points."""
    if len(series) <= max_points:
        return series
    span_seconds = (series.index[-1] - series.index[0]).total_seconds()
    freq = pd.Timedelta(seconds=max(1, span_seconds / max_points))
    return series.resample(freq).mean().dropna()


# (worker, column) -> (fingerprint of the "closed" files behind it, their
# concatenated, ts-sorted series). One entry per family, overwritten in
# place whenever a file closes (~once a day, at midnight) -- see
# `_closed_series`. Without concatenating the whole worker's history from
# scratch on every tick, a plot's per-tick cost stops scaling with how many
# days/weeks the dashboard has been accumulating data for.
_closed_series_cache: dict[tuple[str, str], tuple[tuple, pd.Series]] = {}


def _worker_name(files: list[Path]) -> str:
    match = FILENAME_RE.match(files[0].name)
    return match.group(2)


def _closed_and_open(files: list[Path], start: datetime, end: datetime) -> tuple[list[Path], Path | None]:
    """Split the files overlapping [start, end] into ones that can never
    change again ("closed") and, if included, the single newest file across
    the whole worker -- the only one that can still be actively growing."""
    relevant = [p for p in files if start.date() <= file_date(p) <= end.date()]
    if relevant and relevant[-1] == files[-1]:
        return relevant[:-1], relevant[-1]
    return relevant, None


def _closed_series(worker: str, column: str, closed: list[Path]) -> pd.Series:
    """The concatenated, ts-sorted series for `column` across `closed`
    (permanently finished) files.

    Cached per (worker, column) and only rebuilt when the set of closed
    files actually changes -- which happens once a day, when yesterday's
    still-open file rotates out and becomes permanently closed, not on
    every tick.
    """
    fingerprint = tuple((p, p.stat().st_mtime) for p in closed)
    key = (worker, column)
    cached = _closed_series_cache.get(key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    chunks = []
    for path in closed:
        df = _load_file(path)
        if column in df.columns:
            chunks.append(df[["ts", column]])
    # kind="stable": a handful of genuinely-simultaneous duplicate
    # timestamps do occur; the default quicksort would order them
    # differently depending on what else is in the array being sorted,
    # which isn't wrong but is needlessly nondeterministic tick-to-tick.
    series = (
        pd.concat(chunks, ignore_index=True).set_index("ts")[column].sort_index(kind="stable")
        if chunks else pd.Series(dtype="float64")
    )
    _closed_series_cache[key] = (fingerprint, series)
    return series


def _column_series(files: list[Path], column: str, start: datetime, end: datetime) -> pd.Series:
    """The full-resolution, ts-indexed series for `column` across every file
    overlapping [start, end] (not yet sliced to it), stitching the cached
    closed-files series together with a fresh read of the one file that can
    still be growing.

    Closed and open pieces are each individually ts-sorted and cover
    non-overlapping, chronologically ordered spans (closed files finish
    strictly before the open one starts), so concatenating them needs no
    additional full-range sort.
    """
    closed, open_file = _closed_and_open(files, start, end)
    closed_series = _closed_series(_worker_name(files), column, closed)

    if open_file is None:
        return closed_series

    df = _load_file(open_file)
    if column not in df.columns:
        return closed_series

    open_series = df.set_index("ts")[column].sort_index(kind="stable")
    return pd.concat([closed_series, open_series]) if not closed_series.empty else open_series


def _slice_between(series: pd.Series, start: datetime, end: datetime) -> pd.Series:
    """`series.loc[start:end]`, via binary search on the raw index values
    instead of pandas' label-based `.loc`.

    `.loc` insists on checking the index's uniqueness/monotonicity before it
    can slice, which is free once cached on a long-lived object but costs
    tens of milliseconds on the *fresh* concatenation `_column_series`
    produces on every call -- even though that array is already sorted and
    binary search only needs exactly that.
    """
    idx = series.index
    lo = idx.searchsorted(start, side="left")
    hi = idx.searchsorted(end, side="right")
    return series.iloc[lo:hi]


def column_extent(
    files: list[Path],
    column: str,
    start: datetime,
    end: datetime,
    lower_quantile: float = 0.01,
    upper_quantile: float = 0.99,
) -> tuple[float, float] | None:
    """Robust min/max for sizing a y-range slider.

    Uses the 1st/99th percentile rather than the true min/max so a handful of
    outlier spikes (sensor glitches, startup transients) don't blow out the
    slider range and make it useless for the bulk of the data.
    """
    if not files:
        return None
    values = _slice_between(_column_series(files, column, start, end), start, end).dropna()
    if values.empty:
        return None
    lo, hi = values.quantile([lower_quantile, upper_quantile])
    return float(lo), float(hi)


def load_series(
    files: list[Path], column: str, start: datetime, end: datetime, downsample_result: bool = True
) -> pd.Series:
    """Load a single column across the files overlapping [start, end].

    Pass `downsample_result=False` to get the full-resolution series -- needed
    before computing a rolling average, since averaging *after* downsampling
    would smooth over bins that may already span minutes, making a short
    window meaningless.
    """
    if not files:
        return pd.Series(dtype="float64")
    series = _slice_between(_column_series(files, column, start, end), start, end).dropna()
    return downsample(series) if downsample_result else series
