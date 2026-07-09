"""Discovery, caching, and loading helpers for CPC telemetry CSVs in `data/`."""
from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_([A-Za-z]+)_telemetry\.csv$")

MAX_PLOT_POINTS = 5000

# path -> (mtime, dataframe). Shared process-wide so every session benefits
# from the same cache, and a file is only re-parsed once its mtime changes
# (i.e. after the background refresh actually pulls new rows into it).
_file_cache: dict[Path, tuple[float, pd.DataFrame]] = {}


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


def worker_columns(files: list[Path]) -> list[str]:
    header = pd.read_csv(files[0], nrows=0).columns.tolist()
    return [c for c in header if c != "ts"]


def _load_file(path: Path) -> pd.DataFrame:
    mtime = path.stat().st_mtime
    cached = _file_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    df = pd.read_csv(path)
    # A handful of rows have a blank or malformed `ts` (e.g. two timestamps
    # glued together) from glitches in the live logger; drop rather than crash.
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df = df.dropna(subset=["ts"])
    _file_cache[path] = (mtime, df)
    return df


def downsample(series: pd.Series, max_points: int = MAX_PLOT_POINTS) -> pd.Series:
    """Resample long series so the browser never has to render >max_points."""
    if len(series) <= max_points:
        return series
    span_seconds = (series.index[-1] - series.index[0]).total_seconds()
    freq = pd.Timedelta(seconds=max(1, span_seconds / max_points))
    return series.resample(freq).mean().dropna()


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
    relevant = [p for p in files if start.date() <= file_date(p) <= end.date()]

    chunks = []
    for path in relevant:
        df = _load_file(path)
        if column not in df.columns:
            continue
        values = df.loc[(df["ts"] >= start) & (df["ts"] <= end), column].dropna()
        if not values.empty:
            chunks.append(values)

    if not chunks:
        return None

    all_values = pd.concat(chunks, ignore_index=True)
    lo, hi = all_values.quantile([lower_quantile, upper_quantile])
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
    relevant = [p for p in files if start.date() <= file_date(p) <= end.date()]

    frames = []
    for path in relevant:
        df = _load_file(path)
        if column in df.columns:
            frames.append(df[["ts", column]])
    if not frames:
        return pd.Series(dtype="float64")

    combined = pd.concat(frames, ignore_index=True).set_index("ts").sort_index()
    series = combined.loc[start:end, column].dropna()
    return downsample(series) if downsample_result else series
