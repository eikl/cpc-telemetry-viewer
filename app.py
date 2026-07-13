"""Panel web dashboard for CPC telemetry.

Run with:
    panel serve app.py --show --autoreload

Data is refreshed in the background by repeatedly invoking
pull_telemetry.sh (see data_refresh.py); the plot picks up new rows
automatically without a page reload.
"""
from __future__ import annotations

from datetime import timedelta

import hvplot.pandas  # noqa: F401 - registers the .hvplot accessor on Series
import panel as pn
import param

import data_refresh
import log_data
import telemetry_data as td

pn.extension(sizing_mode="stretch_width")

REFRESH_INTERVAL_SECONDS = 1

LINE_COLOR = "#2a78d6"   # dataviz palette, categorical slot 1 (blue) -- the rolling average
RAW_COLOR = "#c3c2b7"    # baseline/axis gray -- de-emphasized raw signal behind the average
GRID_COLOR = "#e1e0d9"   # hairline gridline


def _set_objects_if_changed(param_obj, objects: list) -> None:
    """Only reassign a Selector's `.objects` when the list actually changed.

    Reassigning unconditionally pushes a fresh `options` update to the
    dropdown widget every time it runs, even when the list is identical --
    which snaps an open dropdown closed in the browser. Since this ran on
    every periodic refresh tick, it made picking a new worker/column while
    live mode was ticking every second nearly impossible.
    """
    if list(param_obj.objects) != list(objects):
        param_obj.objects = objects


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

        _set_objects_if_changed(self.param.worker, workers)
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
            _set_objects_if_changed(self.param.column, columns)
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
        _set_objects_if_changed(self.param.worker, workers)

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


class ExtraPlot(param.Parameterized):
    """A secondary plot shown under the main one. Deliberately minimal (just
    a worker/column pick, auto-fit y-range) -- it shares the main dashboard's
    time window (date_range) and live-mode/refresh cadence via the dotted
    cross-object dependency in `view()`'s `@param.depends`, rather than
    duplicating the date-range/live-mode/rolling-average controls, so adding
    several of these is a quick way to compare signals over the same window."""
    dashboard = param.Parameter()
    worker = param.Selector(default=None, objects=[])
    column = param.Selector(default=None, objects=[])

    def __init__(self, dashboard: TelemetryDashboard, **params):
        super().__init__(dashboard=dashboard, **params)
        workers = sorted(dashboard._files_by_worker)
        _set_objects_if_changed(self.param.worker, workers)
        if workers:
            self.worker = workers[0]  # triggers _on_worker_change below

    @param.depends("worker", watch=True)
    def _on_worker_change(self):
        files = self.dashboard._files_by_worker.get(self.worker, [])
        columns = td.worker_columns(files) if files else []
        _set_objects_if_changed(self.param.column, columns)
        if columns and self.column not in columns:
            self.column = columns[0]

    @param.depends("worker", "column", "dashboard.date_range", "dashboard.refresh_tick")
    def view(self):
        if not self.worker or not self.column or not self.dashboard.date_range:
            return pn.pane.Alert("Pick a worker and column.", alert_type="warning")

        start, end = self.dashboard.date_range
        files = self.dashboard._files_by_worker.get(self.worker, [])
        series = td.load_series(files, self.column, start, end)
        if series.empty:
            return pn.pane.Alert("No data points in the selected range.", alert_type="warning")

        extent = td.column_extent(files, self.column, start, end)
        y_lo, y_hi = extent if extent is not None else (float(series.min()), float(series.max()))
        pad = (y_hi - y_lo) * 0.05 or 1.0

        return series.hvplot.line(x="ts", color=LINE_COLOR, line_width=2, hover=True).opts(
            xlabel="Time",
            ylabel=self.column,
            title=f"{self.worker} — {self.column}",
            height=300,
            responsive=True,
            show_grid=True,
            gridstyle={"grid_line_color": GRID_COLOR},
            ylim=(y_lo - pad, y_hi + pad),
        )


class LogViewer(param.Parameterized):
    service = param.Selector(default=None, objects=[])
    log_file = param.Selector(default=None, objects=[])
    max_lines = param.Integer(default=500, bounds=(50, 20000))
    levels = param.ListSelector(default=list(log_data.LEVELS), objects=list(log_data.LEVELS))
    search = param.String(default="")
    live_tail = param.Boolean(default=False)
    refresh_tick = param.Integer(default=0)

    def __init__(self, **params):
        super().__init__(**params)
        self._files_by_service: dict[str, list[tuple[str, str]]] = {}
        self.error: str | None = None
        self.refresh_services()

    def refresh_services(self) -> None:
        try:
            self._files_by_service = log_data.list_log_files()
            self.error = None
        except Exception as exc:
            self.error = str(exc)
            return

        services = sorted(self._files_by_service)
        _set_objects_if_changed(self.param.service, services)
        if self.service not in services and services:
            self.service = services[0]  # triggers _on_service_change below
        else:
            self._refresh_file_list()

    @param.depends("service", watch=True)
    def _on_service_change(self):
        self._refresh_file_list()

    def _refresh_file_list(self) -> None:
        entries = self._files_by_service.get(self.service, [])
        labels = [label for label, _ in entries]
        _set_objects_if_changed(self.param.log_file, labels)
        if self.log_file not in labels and labels:
            self.log_file = labels[0]

    def tick(self) -> None:
        """Called on every periodic tick while a session is open; only
        actually re-fetches when live-tail is on, to avoid hammering the
        remote with an SSH call every second regardless."""
        if self.live_tail:
            self.refresh_tick += 1

    def render(self) -> str:
        """Plain string, not a Panel component -- callers push this into a
        single persistent HTML pane's `.object` (see build_app's
        `_sync_log_pane`) instead of swapping in a new pane every tick, which
        is what caused the visible flash during live tail."""
        if self.error:
            return log_data.render_message_html(f"Could not list remote logs: {self.error}")
        if not self.service or not self.log_file:
            return log_data.render_message_html("No log files found on the remote.")

        path = dict(self._files_by_service.get(self.service, [])).get(self.log_file)
        if path is None:
            return log_data.render_message_html("Selected log file is no longer available.")

        try:
            text = log_data.fetch_lines(path, self.max_lines, self.search)
        except Exception as exc:
            return log_data.render_message_html(f"Could not fetch log: {exc}")

        return log_data.render_log_html(text, self.levels)


def status_markdown() -> str:
    status = data_refresh.get_status()
    last_run = status["last_run"]
    when = last_run.strftime("%H:%M:%S") if last_run else "never"
    icon = {True: "✅", False: "⚠️", None: "⏳"}[status["ok"]]
    # "pull succeeded" is redundant with the checkmark; only show the message
    # when it says something the icon doesn't (a failure, or a skipped run).
    detail = "" if status["message"] == "pull succeeded" else f" — {status['message']}"
    return f"{icon} Last pull {when}{detail}"


def build_app() -> pn.template.FastListTemplate:
    dashboard = TelemetryDashboard()
    log_viewer = LogViewer()
    data_refresh.start_background_refresh(REFRESH_INTERVAL_SECONDS)

    # Sized to its content and placed in the header bar, not the sidebar --
    # a whole reserved sidebar column for one short status line wasted a lot
    # of horizontal space, especially now that the actual controls live in
    # each tab rather than the sidebar.
    status_pane = pn.pane.Markdown(
        status_markdown(), margin=(15, 15), styles={"color": "white"}, width=320, sizing_mode="fixed"
    )

    def _tick():
        dashboard.refresh()
        log_viewer.tick()
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
            # A dropdown's open popup gets closed by Bokeh/the browser when
            # *any* model on the page patches, which happens every refresh
            # tick -- in live mode that left about one second to pick a new
            # worker/column before the menu snapped shut. RadioButtonGroup
            # has no popup to close, so it can't be interrupted that way.
            "worker": {"widget_type": pn.widgets.RadioButtonGroup, "orientation": "vertical", "button_type": "default"},
            "column": {"widget_type": pn.widgets.RadioButtonGroup, "orientation": "vertical", "button_type": "default"},
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

    extra_plots_column = pn.Column()

    def _add_plot(_event=None):
        plot = ExtraPlot(dashboard)
        remove_button = pn.widgets.Button(name="✕ Remove", button_type="light", width=100)
        block = pn.Column(
            pn.Row(
                # RadioButtonGroup, not Select -- see the comment on `controls`
                # above for why a dropdown popup doesn't survive live mode.
                pn.widgets.RadioButtonGroup.from_param(plot.param.worker, name="Worker", width=220),
                pn.widgets.RadioButtonGroup.from_param(plot.param.column, name="Column", width=220),
                remove_button,
            ),
            plot.view,
            pn.layout.Divider(),
        )
        remove_button.on_click(lambda _event: extra_plots_column.remove(block))
        extra_plots_column.append(block)

    add_plot_button = pn.widgets.Button(name="+ Add plot", button_type="primary", width=150)
    add_plot_button.on_click(_add_plot)

    telemetry_tab = pn.Row(
        pn.Column(controls, width=280),
        pn.Column(dashboard.view, pn.layout.Divider(), add_plot_button, extra_plots_column),
    )

    log_controls = pn.Param(
        log_viewer,
        parameters=["service", "log_file", "max_lines", "levels", "search", "live_tail"],
        widgets={
            # Same reasoning as the Telemetry tab's `controls`: a dropdown
            # popup gets closed by any page patch, which live-tail causes
            # every tick, so use a widget with no popup to close instead.
            "service": {"widget_type": pn.widgets.RadioButtonGroup, "orientation": "vertical", "button_type": "default"},
            "log_file": {"widget_type": pn.widgets.RadioButtonGroup, "orientation": "vertical", "button_type": "default"},
            "levels": pn.widgets.CheckBoxGroup,
            "search": {"widget_type": pn.widgets.TextInput, "placeholder": "Filter (remote grep, case-insensitive)"},
            "live_tail": {"widget_type": pn.widgets.Switch, "name": "Live tail"},
        },
        show_name=False,
        sizing_mode="stretch_width",
    )
    refresh_files_button = pn.widgets.Button(name="Refresh file list", button_type="default")
    refresh_files_button.on_click(lambda _event: log_viewer.refresh_services())

    # One persistent pane whose `.object` gets updated in place, rather than
    # a `@param.depends` method returning a fresh `pn.pane.HTML` every tick --
    # swapping in a whole new pane (plus its embedded <style>, before that
    # moved to the template's raw_css) is what caused live tail to flash.
    log_html_pane = pn.pane.HTML(sizing_mode="stretch_width")

    def _sync_log_pane(*_events):
        log_html_pane.object = log_viewer.render()

    log_viewer.param.watch(_sync_log_pane, ["service", "log_file", "max_lines", "levels", "search", "refresh_tick"])
    _sync_log_pane()

    logs_tab = pn.Row(
        pn.Column(log_controls, refresh_files_button, width=280),
        log_html_pane,
    )

    template = pn.template.FastListTemplate(
        title="CPC Telemetry",
        header=[status_pane],
        main=[pn.Tabs(("Telemetry", telemetry_tab), ("Logs", logs_tab))],
        raw_css=[log_data.LOG_VIEWER_CSS],
    )
    return template


build_app().servable(title="CPC Telemetry")
