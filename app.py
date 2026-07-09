"""Panel web dashboard for CPC telemetry.

Run with:
     

Data is refreshed in the background by repeatedly invoking
scripts/pull_telemetry.sh (see data_refresh.py); the plot picks up new rows
automatically without a page reload.
"""
from __future__ import annotations

from datetime import timedelta

import hvplot.pandas  # noqa: F401 - registers the .hvplot accessor on Series
import panel as pn
import param

import data_refresh
import telemetry_data as td

pn.extension(sizing_mode="stretch_width")

REFRESH_INTERVAL_SECONDS = 1

LINE_COLOR = "#2a78d6"   # dataviz palette, categorical slot 1 (blue) -- the rolling average
RAW_COLOR = "#c3c2b7"    # baseline/axis gray -- de-emphasized raw signal behind the average
GRID_COLOR = "#e1e0d9"   # hairline gridline


class TelemetryDashboard(param.Parameterized):
    worker = param.Selector(default=None, objects=[])
    column = param.Selector(default=None, objects=[])
    date_range = param.DateRange(default=None)
    # y_range's slider bounds are tracked in a plain sibling param (y_bounds)
    # rather than via param.Range's own `.bounds`. Param batches/reorders
    # nested attribute changes made while already dispatching a watcher (e.g.
    # worker -> column -> y-bounds cascades through here), which can push a
    # new y_range *value* to the EditableRangeSlider before its *bounds* have
    # caught up and made that value valid, raising a ValueError. Keeping them
    # as two plain values and pushing both to the widget together in one call
    # (see build_app's _sync_y_widget) sidesteps that race entirely.
    y_bounds = param.Tuple(default=None, length=2)
    y_range = param.Range(default=None)
    log_scale = param.Boolean(default=False)
    live_mode = param.Boolean(default=False)
    live_window_seconds = param.Integer(default=60, bounds=(1, 24 * 3600))
    rolling_avg = param.Boolean(default=False)
    rolling_window_seconds = param.Integer(default=10, bounds=(1, 3600))
    refresh_tick = param.Integer(default=0)

    def __init__(self, **params):
        super().__init__(**params)
        self._files_by_worker: dict[str, list] = {}
        self._syncing = False
        self._last_fingerprint: tuple | None = None
        self._rescan(extend_bounds_only=False)
        self._last_fingerprint = self._fingerprint(self._files_by_worker.get(self.worker, []))

    def _rescan(self, extend_bounds_only: bool) -> None:
        self._files_by_worker = td.discover_files()
        workers = sorted(self._files_by_worker)
        if not workers:
            return

        self.param.worker.objects = workers
        if self.worker not in workers:
            self.worker = workers[0]  # triggers _on_worker_change below
        else:
            self._sync_worker(extend_bounds_only)

    @param.depends("worker", watch=True)
    def _on_worker_change(self):
        self._sync_worker(extend_bounds_only=False)

    def _sync_worker(self, extend_bounds_only: bool) -> None:
        """Recompute the column list/selection, its y-range, and the date
        bounds for the current worker, applied as one batched update.

        Setting `column.objects` below makes the column widget reset its own
        value if the old one is invalid, which re-enters `_on_column_change`
        mid-cascade. The `_syncing` guard stops that nested call from
        recomputing y-bounds a second time against still-stale widget state
        (which is what caused a bounds/value mismatch to reach the browser).
        """
        files = self._files_by_worker.get(self.worker, [])
        if not files:
            return

        self._syncing = True
        try:
            columns = td.worker_columns(files)
            self.param.column.objects = columns
            column = self.column if self.column in columns else columns[0]

            start, end = td.worker_date_span(files)
            self.param.date_range.bounds = (start, end)

            if self.live_mode:
                date_range, y_bounds, y_range = self._live_window(files, column)
                if date_range is None:
                    date_range = self.date_range or (start, end)
            elif self.date_range is None:
                date_range = (start, end)
                y_bounds, y_range = self._y_bounds_and_range(files, column, start, end, extend_bounds_only)
            elif extend_bounds_only:
                date_range = self.date_range
                y_bounds, y_range = self._y_bounds_and_range(files, column, start, end, extend_bounds_only)
            else:
                # Worker/column changed: keep the user's chosen x-range
                # instead of snapping back to the full span, just clamp it.
                cur_start, cur_end = self.date_range
                date_range = (min(max(cur_start, start), end), max(min(cur_end, end), start))
                y_bounds, y_range = self._y_bounds_and_range(files, column, start, end, extend_bounds_only)

            updates = {"column": column, "date_range": date_range}
            if y_bounds is not None:
                updates["y_bounds"] = y_bounds
                updates["y_range"] = y_range
            self.param.update(**updates)
        finally:
            self._syncing = False

    @param.depends("column", watch=True)
    def _on_column_change(self):
        if self._syncing:
            return  # already being handled by _sync_worker
        files = self._files_by_worker.get(self.worker, [])
        if not files:
            return

        if self.live_mode:
            date_range, y_bounds, y_range = self._live_window(files, self.column)
            if date_range is None:
                return
            self.param.update(date_range=date_range, y_bounds=y_bounds, y_range=y_range)
            return

        start, end = td.worker_date_span(files)
        y_bounds, y_range = self._y_bounds_and_range(files, self.column, start, end, extend_bounds_only=False)
        if y_bounds is not None:
            self.param.update(y_bounds=y_bounds, y_range=y_range)

    @param.depends("live_mode", "live_window_seconds", watch=True)
    def _on_live_settings_change(self):
        if self._syncing or not self.live_mode:
            return
        files = self._files_by_worker.get(self.worker, [])
        if not files:
            return
        date_range, y_bounds, y_range = self._live_window(files, self.column)
        if date_range is None:
            return
        self.param.update(date_range=date_range, y_bounds=y_bounds, y_range=y_range)

    def _live_window(self, files, column):
        """The (date_range, y_bounds, y_range) for the last `live_window_seconds`
        of *data* (anchored to the latest timestamp actually on disk, not
        wall-clock time, since the background pull can lag behind real time)."""
        latest = td.latest_timestamp(files)
        if latest is None:
            return None, None, None
        start = latest - timedelta(seconds=self.live_window_seconds)
        y_bounds, y_range = self._y_bounds_and_range(files, column, start, latest, extend_bounds_only=False)
        return (start, latest), y_bounds, y_range

    def _y_bounds_and_range(self, files, column, start, end, extend_bounds_only: bool):
        """Compute the y-bounds for `column` and the value the y_range slider
        should hold (the fresh bounds, or the user's current selection if
        we're only widening bounds on a periodic refresh)."""
        extent = td.column_extent(files, column, start, end)
        if extent is None:
            return None, None
        lo, hi = extent
        pad = (hi - lo) * 0.05 or 1.0
        bounds = (lo - pad, hi + pad)
        if extend_bounds_only and self.y_range is not None and self.column == column:
            return bounds, self.y_range
        return bounds, bounds

    def refresh(self) -> None:
        """Called on every periodic tick.

        Re-globbing the data directory is cheap, but `_sync_worker` (which
        recomputes y-bounds) and `view()` (which `refresh_tick` triggers, and
        which reloads+aggregates every row in the current range) are not --
        and get slower over a long-running session simply because today's
        file keeps growing. So skip that expensive part entirely unless a
        file relevant to what's currently on screen actually changed size
        since the last tick (checked via mtime, never by parsing the CSV).
        """
        self._files_by_worker = td.discover_files()
        workers = sorted(self._files_by_worker)
        if not workers:
            return
        self.param.worker.objects = workers

        if self.worker not in workers:
            self.worker = workers[0]  # triggers _on_worker_change -> full sync
            self.refresh_tick += 1
            return

        files = self._files_by_worker.get(self.worker, [])
        fingerprint = self._fingerprint(files)
        if fingerprint == self._last_fingerprint:
            return
        self._last_fingerprint = fingerprint

        self._sync_worker(extend_bounds_only=True)
        self.refresh_tick += 1

    def _fingerprint(self, files: list) -> tuple:
        """Cheap (path, mtime) signature for the files relevant to the
        current selection, to detect whether anything actually changed on
        disk without touching file contents."""
        if not files:
            return ()
        if self.live_mode or self.date_range is None:
            relevant = files[-1:]  # only the newest file matters for a live tail
        else:
            start, end = self.date_range
            relevant = [p for p in files if start.date() <= td.file_date(p) <= end.date()]
        return tuple((str(p), p.stat().st_mtime) for p in relevant)

    @param.depends(
        "worker", "column", "date_range", "y_range", "log_scale",
        "rolling_avg", "rolling_window_seconds", "refresh_tick",
    )
    def view(self):
        if not self.worker or not self.column or not self.date_range or not self.y_range:
            return pn.pane.Alert("No telemetry data found in `data/` yet.", alert_type="warning")

        start, end = self.date_range
        files = self._files_by_worker.get(self.worker, [])

        y_lo, y_hi = self.y_range
        if self.log_scale:
            y_lo = max(y_lo, 1e-6)  # a log axis can't start at/below zero

        if self.rolling_avg:
            # Smooth on the full-resolution series *before* downsampling for
            # render -- averaging after downsampling would smooth over bins
            # that may already span minutes, making a short window meaningless.
            raw = td.load_series(files, self.column, start, end, downsample_result=False)
            if raw.empty:
                return pn.pane.Alert("No data points in the selected range.", alert_type="warning")
            smoothed = raw.rolling(f"{self.rolling_window_seconds}s").mean().dropna()
            plot = (
                td.downsample(raw).hvplot.line(x="ts", color=RAW_COLOR, line_width=1, alpha=0.6, label="Raw", hover=True)
                * td.downsample(smoothed).hvplot.line(
                    x="ts", color=LINE_COLOR, line_width=2,
                    label=f"{self.rolling_window_seconds}s avg", hover=True,
                )
            ).opts(legend_position="top_right")
        else:
            series = td.load_series(files, self.column, start, end)
            if series.empty:
                return pn.pane.Alert("No data points in the selected range.", alert_type="warning")
            plot = series.hvplot.line(x="ts", color=LINE_COLOR, line_width=2, hover=True)

        return plot.opts(
            xlabel="Time",
            ylabel=self.column,
            title=f"{self.worker} — {self.column}",
            height=440,
            responsive=True,
            show_grid=True,
            gridstyle={"grid_line_color": GRID_COLOR},
            logy=self.log_scale,
            ylim=(y_lo, y_hi),
        )


def status_markdown() -> str:
    status = data_refresh.get_status()
    last_run = status["last_run"]
    when = last_run.strftime("%Y-%m-%d %H:%M:%S") if last_run else "never"
    icon = {True: "✅", False: "⚠️", None: "⏳"}[status["ok"]]
    return f"{icon} **Last pull:** {when}  \n{status['message']}"


def build_app() -> pn.template.FastListTemplate:
    dashboard = TelemetryDashboard()
    data_refresh.start_background_refresh(REFRESH_INTERVAL_SECONDS)

    status_pane = pn.pane.Markdown(status_markdown(), sizing_mode="stretch_width")

    def _tick():
        dashboard.refresh()
        status_pane.object = status_markdown()

    pn.state.add_periodic_callback(_tick, period=REFRESH_INTERVAL_SECONDS * 1000)

    controls = pn.Param(
        dashboard,
        parameters=[
            "worker",
            "column",
            "date_range",
            "live_mode",
            "live_window_seconds",
            "y_range",
            "log_scale",
            "rolling_avg",
            "rolling_window_seconds",
        ],
        widgets={
            "date_range": pn.widgets.DatetimeRangePicker,
            "live_mode": {"widget_type": pn.widgets.Switch, "name": "Live (last N seconds)"},
            "live_window_seconds": {"widget_type": pn.widgets.IntInput, "name": "Window (s)"},
            "y_range": pn.widgets.EditableRangeSlider,
            "log_scale": pn.widgets.Switch,
            "rolling_avg": {"widget_type": pn.widgets.Switch, "name": "Rolling average"},
            "rolling_window_seconds": {"widget_type": pn.widgets.IntInput, "name": "Avg window (s)"},
        },
        show_name=False,
        sizing_mode="stretch_width",
    )

    # y_range's widget needs start/end (from y_bounds) and value (y_range)
    # pushed together in one call -- see the comment on TelemetryDashboard.y_bounds.
    y_widget = controls._widgets["y_range"]

    def _sync_y_widget(*_events):
        if dashboard.y_bounds is None or dashboard.y_range is None:
            return
        lo, hi = dashboard.y_bounds
        y_widget.param.update(start=lo, end=hi, value=dashboard.y_range)

    dashboard.param.watch(_sync_y_widget, ["y_bounds", "y_range"])
    _sync_y_widget()

    # Manually editing the date range makes no sense while live mode keeps
    # overwriting it every tick, so grey it out while live mode is on.
    date_widget = controls._widgets["date_range"]

    def _sync_date_widget(*_events):
        date_widget.disabled = dashboard.live_mode

    dashboard.param.watch(_sync_date_widget, ["live_mode"])
    _sync_date_widget()

    template = pn.template.FastListTemplate(
        title="CPC Telemetry",
        sidebar=[controls, pn.layout.Divider(), status_pane],
        main=[dashboard.view],
    )
    return template


build_app().servable(title="CPC Telemetry")
