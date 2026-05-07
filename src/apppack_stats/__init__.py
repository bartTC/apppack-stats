"""
Render live endpoint latency statistics from AppPack access logs.

This module is the executable heart of ``apppack-stats``. It shells out to
``apppack logs --raw --follow``, parses the JSON access-log lines it cares
about, aggregates them into per-endpoint buckets, and presents the result in a
Textual ``DataTable``.

The implementation is split between a background reader thread that ingests log
lines and a foreground Textual app that periodically refreshes the table from a
shared in-memory snapshot. The refresh path is intentionally careful about
avoiding full table rebuilds during normal operation because the UI stays much
more responsive when only changed rows are touched.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import statistics
import subprocess
import sys
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import IO, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header

from apppack_stats.extractors import extract_request, looks_like_apache_clf

__version__ = "0.6.0"

# Order matters: UUIDs and hex hashes before bare ints.
_UUID_RE = re.compile(
    r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?=/|$)",
    re.IGNORECASE,
)
_HEX_RE = re.compile(r"/[0-9a-f]{16,}(?=/|$)", re.IGNORECASE)
_INT_RE = re.compile(r"/\d+(?=/|$)")
_CLIENT_ERROR_MIN = 400
_SERVER_ERROR_MIN = 500
_MIN_SAMPLES_FOR_P95 = 2
_P95_QUANTILES = 20
_P95_INDEX = 18


def normalize_path(path: str) -> str:
    """
    Collapse variable path segments into stable endpoint placeholders.

    The live table is most useful when it groups requests by logical endpoint
    rather than by every concrete resource identifier. This function rewrites
    UUIDs, long hex tokens, and bare integer path segments into placeholders so
    repeated requests to the same handler land in one bucket.

    The substitutions are ordered from most specific to least specific to avoid
    broad numeric matching swallowing UUID fragments or hash-like identifiers.
    """
    path = _UUID_RE.sub("/<uuid>", path)
    path = _HEX_RE.sub("/<hash>", path)
    return _INT_RE.sub("/<id>", path)


@dataclass
class Bucket:
    """
    Accumulate request counts, error counts, and raw timings for one endpoint.

    Each bucket represents a single ``(method, path)`` group in the final table.
    The class stores the raw microsecond timings rather than incremental summary
    values so the UI can render average, p95, and max without losing fidelity.

    This is simple rather than memory-optimized on purpose: the expected use
    case is an interactive troubleshooting session with a manageable amount of
    recent traffic, where correctness and clarity matter more than squeezing the
    last byte out of the accumulator.
    """

    count: int = 0
    errors_4xx: int = 0
    errors_5xx: int = 0
    times_us: list[int] = field(default_factory=list)

    def add(self, time_us: int, status: int) -> None:
        """
        Record one completed request in this bucket.

        Status codes are split into separate 4xx and 5xx counters because those
        categories typically carry different operational meaning. The timing is
        appended to the raw sample list so later percentile calculations can use
        the full distribution instead of an approximation.
        """
        self.count += 1
        self.times_us.append(time_us)
        if _CLIENT_ERROR_MIN <= status < _SERVER_ERROR_MIN:
            self.errors_4xx += 1
        elif status >= _SERVER_ERROR_MIN:
            self.errors_5xx += 1

    @property
    def avg_ms(self) -> float:
        """
        Return the arithmetic mean latency in milliseconds.

        The live UI rounds this value for display, but the property itself keeps
        the underlying float precision so sorts and exports can use the exact
        aggregate instead of the human-formatted string.
        """
        return statistics.fmean(self.times_us) / 1000

    @property
    def p95_ms(self) -> float:
        """
        Return the 95th percentile latency in milliseconds.

        Buckets with fewer than two samples do not have a meaningful percentile
        distribution yet, so the single observed timing is returned directly.
        Once enough samples exist, the calculation uses the standard-library
        quantile helper over the raw microsecond timings.
        """
        if len(self.times_us) < _MIN_SAMPLES_FOR_P95:
            return self.times_us[0] / 1000
        return statistics.quantiles(self.times_us, n=_P95_QUANTILES)[_P95_INDEX] / 1000

    @property
    def max_ms(self) -> float:
        """
        Return the slowest observed latency in milliseconds.

        This remains a direct maximum rather than a smoothed statistic because
        single outliers are often exactly what an operator wants to notice.
        """
        return max(self.times_us) / 1000


class Stats:
    """
    Own the shared aggregation state fed by the log reader thread.

    The Textual UI and the background log-consumer thread both interact with the
    same ``Stats`` instance. A coarse lock protects the bucket mapping and the
    parsed-line counters while still keeping the ingestion logic lightweight.

    This object intentionally accepts raw log lines instead of pre-parsed
    records. That keeps the reader thread self-contained and lets the rest of
    the application treat ``Stats`` as the single place where "interesting log
    line" semantics live.
    """

    def __init__(self, *, normalize: bool) -> None:
        """
        Initialize an empty aggregation state for one live monitoring session.

        The ``normalize`` flag controls whether future ingested lines collapse
        resource-specific path segments into endpoint placeholders before they
        are bucketed.
        """
        self.normalize = normalize
        self.buckets: dict[tuple[str, str], Bucket] = defaultdict(Bucket)
        self.total_lines = 0
        self.parsed_lines = 0
        self.unsupported_clf_lines = 0
        self.started = monotonic()
        self.lock = threading.Lock()

    def ingest(self, line: str) -> None:
        """
        Parse one raw log line and fold it into the aggregate state.

        Most lines from ``apppack logs`` are ignored. Non-JSON output, malformed
        JSON, and payloads that do not match a known access-log shape simply do
        not contribute to the table. ``total_lines`` still increases for every
        input line so the title bar can show how much of the stream was relevant.

        Normalization happens before the bucket lookup so toggling that mode
        changes the grouping semantics rather than merely changing display text.
        """
        self.total_lines += 1
        line = line.strip()
        if not line:
            return
        if line[0] != "{":
            # Plain-text Apache/NCSA access logs land here. We can't extract
            # latency from them, but we count detections so the app can exit
            # with a clear configuration hint instead of staying empty forever.
            if looks_like_apache_clf(line):
                self.unsupported_clf_lines += 1
            return
        try:
            payload = json.loads(line)
        except ValueError:
            return
        extracted = extract_request(payload)
        if extracted is None:
            return
        method, path, time_us, status = extracted

        if self.normalize:
            path = normalize_path(path)

        with self.lock:
            self.buckets[(method, path)].add(time_us, status)
            self.parsed_lines += 1


# Bail out once we've clearly seen Apache/NCSA text logs and no parseable
# JSON. The thresholds let one or two stray lines slip past without exiting,
# but a sustained stream of timing-less lines triggers the configuration hint.
_CLF_BAIL_MIN_TOTAL = 20
_CLF_BAIL_MIN_DETECTED = 10

_CLF_ERROR_MESSAGE = """\
error: input looks like Apache/NCSA Common or Combined Log Format access logs.

These lines contain status codes and paths but no request-time field, so
apppack-stats cannot compute latency statistics from them.

apppack-stats reads JSON access logs containing a request-time field. Two
supported shapes:

  AppPack default (microseconds):
    {"method": "GET", "path": "/foo", "status": 200, "response_time_us": 1234}

  gunicorn structlog (seconds, float):
    {"request_method": "GET", "request_path": "/foo",
     "response_status": 200, "response_time": 0.012}

Configure your app server to emit JSON access logs in one of these shapes
(see your framework's structured-logging docs, or gunicorn's --access-logformat
with a JSON formatter) and rerun apppack-stats.
"""


# Column key -> sort key over (key, bucket) tuples.
_SORT_KEYS: dict[str, callable] = {
    "method": lambda kv: kv[0][0],
    "path": lambda kv: kv[0][1],
    "count": lambda kv: kv[1].count,
    "avg": lambda kv: kv[1].avg_ms,
    "p95": lambda kv: kv[1].p95_ms,
    "max": lambda kv: kv[1].max_ms,
    "4xx": lambda kv: kv[1].errors_4xx,
    "5xx": lambda kv: kv[1].errors_5xx,
}

_NUMERIC_COLS = {"count", "avg", "p95", "max", "4xx", "5xx"}


def _truncate(s: str, width: int) -> str:
    """
    Trim a string to a display width budget using a single trailing ellipsis.

    The table reserves fixed space for numeric columns and lets the path column
    absorb the remaining width. Truncation is therefore a presentation concern,
    not a data-loss concern: the underlying bucket key stays untouched and only
    the rendered cell content is shortened.
    """
    if width <= 1 or len(s) <= width:
        return s
    return s[: width - 1] + "…"


def _copy_to_system_clipboard(text: str) -> bool:
    """
    Pipe ``text`` into the first available platform clipboard binary.

    OSC 52 escapes are unreliable across terminal/tmux setups, so the app
    prefers a native clipboard tool when one is on ``PATH``. The candidates are
    ordered so the most common per-platform choice runs first: ``pbcopy`` on
    macOS, then Wayland, then X11. Returns ``True`` only when the binary
    exists and exits cleanly.
    """
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    else:
        candidates = [
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "-b", "-i"],
        ]
    for cmd in candidates:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            proc = subprocess.run(  # noqa: S603
                cmd, input=text, text=True, check=False, capture_output=True
            )
        except OSError:
            continue
        if proc.returncode == 0:
            return True
    return False


def _reader_thread(stats: Stats, stream: IO[str], stop: threading.Event) -> None:
    """
    Drain the subprocess log stream until told to stop or EOF is reached.

    The reader thread keeps the blocking ``stdout`` iteration away from the
    Textual event loop. It exits cooperatively when the stop event is set and
    always sets that event on the way out so the rest of the shutdown path can
    treat either EOF or user-initiated termination as a completed stream.
    """
    for line in stream:
        if stop.is_set():
            break
        stats.ingest(line)
    stop.set()


class StatsApp(App):
    """
    Present the live aggregate state as an interactive Textual table.

    ``StatsApp`` is responsible for translating shared ``Stats`` state into a
    responsive terminal UI. The table supports live sorting, path normalization
    toggling, and row highlighting that follows the same logical endpoint across
    refreshes.

    The refresh logic is deliberately incremental. Rebuilding the entire table on
    every tick made scrolling and mouse interaction feel sluggish, so the app now
    tracks previously rendered rows, updates only changed cells, and re-sorts
    only when required.
    """

    CSS = """
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str, str]]] = [
        ("q", "quit", "Quit"),
        # Override Textual's default Ctrl+C handler (which prints
        # "Keyboard interrupt detected, exiting..."). Priority binding
        # intercepts the keypress before the SIGINT path runs.
        Binding("ctrl+c", "quit", show=False, priority=True),
        ("n", "toggle_normalize", "Toggle path normalization"),
        ("c", "copy_path", "Copy path"),
    ]

    def __init__(
        self,
        stats: Stats,
        *,
        app_name: str,
        refresh: float,
        proc: subprocess.Popen,
    ) -> None:
        """
        Initialize the UI with shared aggregate state and refresh bookkeeping.

        The app caches rendered rows so refreshes can update only changed cells,
        which keeps scrolling responsive even while new log lines keep arriving.
        It also stores the currently selected row key separately from the screen
        row index so highlighting can survive re-sorts.
        """
        super().__init__()
        self.stats = stats
        self.app_name = app_name
        self.refresh_sec = refresh
        self.proc = proc
        self.sort_col = "avg"
        self.sort_reverse = True
        self._rendered_rows: dict[str, tuple[str, ...]] = {}
        self._last_path_width: int | None = None
        self._needs_sort = True
        self._sort_frozen_until = 0.0
        self._selected_row_key: str | None = None
        self._restoring_cursor = False
        self.exit_error: str | None = None

    def compose(self) -> ComposeResult:
        """
        Declare the static widget layout for the app shell.

        The hierarchy stays intentionally small so nearly all runtime work can
        happen inside the table update cycle rather than in a larger widget tree.
        """
        yield Header()
        yield DataTable(zebra_stripes=True, cursor_type="row")
        yield Footer()

    # Fixed widths of every non-path column (see ``on_mount``). Used to
    # compute how much room is left for the Path column on the current
    # terminal so the numeric columns never get pushed offscreen.
    _OTHER_COLS_WIDTH = 8 + 8 + 8 + 8 + 8 + 6 + 6
    # Per-cell padding/borders DataTable reserves around each column,
    # plus the vertical scrollbar lane. Conservative — we'd rather
    # leave a little gap on the right than trip a horizontal scrollbar.
    _COLUMN_OVERHEAD = 24
    _SORT_FREEZE_SEC = 2.0

    def on_mount(self) -> None:
        """
        Create the table schema and start the periodic refresh timer.

        Column widths are chosen so the numeric metrics remain stable and easy to
        scan, while the path column consumes whatever horizontal space is left.
        The first refresh runs immediately so the app paints useful content
        before the interval timer fires.
        """
        table = self.query_one(DataTable)
        table.add_column("Method", key="method", width=8)
        table.add_column("Path", key="path")
        table.add_column("Count", key="count", width=8)
        table.add_column("Avg ms", key="avg", width=8)
        table.add_column("p95 ms", key="p95", width=8)
        table.add_column("Max ms", key="max", width=8)
        table.add_column("4xx", key="4xx", width=6)
        table.add_column("5xx", key="5xx", width=6)
        self.title = f"apppack-stats for {self.app_name}"
        self._update_subtitle()
        self.set_interval(self.refresh_sec, self._tick)
        self._tick()

    def on_resize(self) -> None:
        """
        Refresh immediately when the terminal size changes.

        Path truncation depends on the available width, so a resize needs a new
        render pass even if the underlying data has not changed.
        """
        # Re-render immediately so the path column is recomputed for
        # the new terminal width.
        self._tick()

    def _path_width(self) -> int:
        """
        Compute the remaining width budget for the path column.

        This subtracts the known fixed-width metric columns plus a conservative
        allowance for DataTable padding and scrollbar space. The calculation
        intentionally leaves a little spare room because clipping a numeric
        column is more damaging to usability than truncating the path slightly
        earlier.
        """
        return max(20, self.size.width - self._OTHER_COLS_WIDTH - self._COLUMN_OVERHEAD)

    @staticmethod
    def _row_key(method: str, path: str) -> str:
        """
        Build a stable row identifier for one logical endpoint.

        Textual rows need keys that remain stable across refreshes and re-sorts.
        Combining method and normalized path is enough to uniquely identify an
        aggregate row, and the embedded NUL separator avoids ambiguity between
        adjacent string boundaries.
        """
        return f"{method}\0{path}"

    def _sort_key_from_row(self, row: tuple[object, ...]) -> object:
        """
        Translate a rendered table row back into a sortable value.

        ``DataTable.sort`` operates on rendered cell values rather than the raw
        ``Bucket`` objects, so numeric columns need to be converted back from the
        formatted strings shown to the user. This keeps the sort behavior aligned
        with the current column choice without maintaining a second parallel view
        model just for ordering.
        """
        sort_index = {
            "method": 0,
            "path": 1,
            "count": 2,
            "avg": 3,
            "p95": 4,
            "max": 5,
            "4xx": 6,
            "5xx": 7,
        }[self.sort_col]
        value = row[sort_index]
        if self.sort_col in _NUMERIC_COLS:
            return int(str(value) or "0")
        return value

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """
        Toggle sort state when the user clicks a column header.

        Numeric columns default to descending order because the most interesting
        values are usually the highest ones. A manual header click also clears
        any temporary sort freeze created by row interaction so explicit sorting
        always feels immediate.
        """
        col_key = str(event.column_key.value) if event.column_key else ""
        if col_key not in _SORT_KEYS:
            return
        if col_key == self.sort_col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = col_key
            self.sort_reverse = col_key in _NUMERIC_COLS
        self._sort_frozen_until = 0.0
        self._needs_sort = True
        self._update_subtitle()
        self._tick()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """
        Remember the selected logical row and briefly freeze auto-sorting.

        The highlight should follow the same endpoint, not whichever row later
        occupies the same visual slot. The short sort freeze reduces the feeling
        that the table is fighting the user while they are actively clicking or
        navigating around it.
        """
        self._selected_row_key = str(event.row_key.value)
        if self._restoring_cursor:
            return
        self._sort_frozen_until = monotonic() + self._SORT_FREEZE_SEC

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """
        Apply the same row-tracking behavior to highlight changes as to selection.

        Textual emits row-highlight events during ordinary cursor movement, so
        this method keeps keyboard and mouse navigation consistent with clicks.
        Cursor restoration triggered by the app itself is ignored to avoid
        self-reinforcing freeze windows.
        """
        self._selected_row_key = str(event.row_key.value)
        if self._restoring_cursor:
            return
        self._sort_frozen_until = monotonic() + self._SORT_FREEZE_SEC

    def action_copy_path(self) -> None:
        """
        Copy the highlighted row's path to the system clipboard.

        Prefers a real system clipboard binary (``pbcopy`` on macOS,
        ``wl-copy``/``xclip``/``xsel`` on Linux) because OSC 52 is unreliable —
        many terminals and tmux configurations swallow the escape. Textual's
        ``copy_to_clipboard`` is used as a last-resort fallback so the action
        still does something useful over SSH or in stripped-down environments.
        The full normalized or raw path is copied (not the on-screen truncated
        form) so users get an actionable value.
        """
        if not self._selected_row_key:
            return
        _, _, path = self._selected_row_key.partition("\0")
        if not path:
            return
        if not _copy_to_system_clipboard(path):
            self.copy_to_clipboard(path)
        self.notify(f"Copied: {path}", timeout=2)

    def action_toggle_normalize(self) -> None:
        """
        Flip between normalized and raw path grouping at runtime.

        The underlying aggregate data is not rebuilt eagerly here. Instead, the
        shared ``Stats`` object changes grouping behavior for future ingested
        lines, and the next refresh updates the subtitle and sort state
        accordingly.
        """
        with self.stats.lock:
            self.stats.normalize = not self.stats.normalize
        self._needs_sort = True
        self._update_subtitle()

    def _update_subtitle(self) -> None:
        """
        Refresh the subtitle so it mirrors the current sort and path mode.

        The subtitle is lightweight but important feedback: it explains both the
        active ordering and whether the table is grouping by normalized or raw
        paths, which can otherwise be hard to infer once the stream is moving.
        """
        arrow = "↓" if self.sort_reverse else "↑"
        norm = "" if self.stats.normalize else "  (raw paths)"
        self.sub_title = f"sort: {self.sort_col} {arrow}{norm}"

    def _snapshot_items(
        self,
    ) -> tuple[list[tuple[tuple[str, str], Bucket]], float, int, int]:
        """
        Capture a consistent aggregate snapshot for one refresh tick.

        The reader thread may still be ingesting lines while the UI redraws, so
        the refresh path copies the current buckets and counters under the shared
        lock and then performs all rendering work after releasing it.
        """
        with self.stats.lock:
            items = list(self.stats.buckets.items())
            elapsed = monotonic() - self.stats.started
            total = self.stats.total_lines
            parsed = self.stats.parsed_lines
        items.sort(key=_SORT_KEYS[self.sort_col], reverse=self.sort_reverse)
        return items, elapsed, total, parsed

    def _render_row(
        self, method: str, path: str, bucket: Bucket, *, path_width: int
    ) -> tuple[str, ...]:
        """
        Build the rendered cell values for one aggregate row.

        The row cache stores already formatted values because that is exactly what
        Textual renders and sorts. Centralizing the formatting keeps new-row and
        changed-row handling consistent.
        """
        return (
            method,
            _truncate(path, path_width),
            str(bucket.count),
            f"{bucket.avg_ms:.0f}",
            f"{bucket.p95_ms:.0f}",
            f"{bucket.max_ms:.0f}",
            str(bucket.errors_4xx) if bucket.errors_4xx else "",
            str(bucket.errors_5xx) if bucket.errors_5xx else "",
        )

    def _apply_row_updates(
        self,
        table: DataTable,
        items: list[tuple[tuple[str, str], Bucket]],
        *,
        path_width: int,
    ) -> tuple[set[str], bool]:
        """
        Apply row additions and cell-level updates for the current snapshot.

        The return value is the set of row keys that should exist after the tick
        plus a flag indicating whether the visible table contents changed.
        """
        path_width_changed = path_width != self._last_path_width
        live_row_keys: set[str] = set()
        rows_changed = False
        for (method, path), bucket in items:
            row_key = self._row_key(method, path)
            live_row_keys.add(row_key)
            row = self._render_row(method, path, bucket, path_width=path_width)
            previous_row = self._rendered_rows.get(row_key)
            if row_key not in table.rows:
                table.add_row(*row, key=row_key)
                self._rendered_rows[row_key] = row
                rows_changed = True
                continue
            if previous_row == row and not path_width_changed:
                continue
            for column_key, old_value, new_value in zip(
                _SORT_KEYS, previous_row or (), row, strict=False
            ):
                if old_value != new_value:
                    table.update_cell(
                        row_key, column_key, new_value, update_width=False
                    )
            if previous_row is None:
                for column_key, new_value in zip(_SORT_KEYS, row, strict=False):
                    table.update_cell(
                        row_key, column_key, new_value, update_width=False
                    )
            self._rendered_rows[row_key] = row
            rows_changed = True
        return live_row_keys, rows_changed

    def _prune_missing_rows(self, table: DataTable, live_row_keys: set[str]) -> bool:
        """
        Remove rows that no longer belong in the rendered snapshot.

        When a row disappears under the current grouping or sort state, any
        cached render state and any selection pointing at that row are cleared
        alongside the widget row itself.
        """
        rows_changed = False
        for existing_row_key in list(table.rows):
            existing_key = str(existing_row_key.value)
            if existing_key not in live_row_keys:
                table.remove_row(existing_row_key)
                self._rendered_rows.pop(existing_key, None)
                if existing_key == self._selected_row_key:
                    self._selected_row_key = None
                rows_changed = True
        return rows_changed

    def _maybe_sort_rows(self, table: DataTable, *, rows_changed: bool) -> None:
        """
        Re-sort the table when data changed and interaction is not frozen.

        Sorting pauses briefly while the user is actively interacting with a row
        highlight so the viewport does not feel like it is fighting the user.
        """
        sort_is_frozen = monotonic() < self._sort_frozen_until
        if (rows_changed or self._needs_sort) and not sort_is_frozen:
            table.sort(key=self._sort_key_from_row, reverse=self.sort_reverse)
            self._needs_sort = False

    def _restore_selected_row(self, table: DataTable) -> None:
        """
        Move the cursor back onto the same logical row after updates.

        Selection follows the row key rather than the previous screen position.
        Cursor moves triggered by this restoration are marked so they do not
        create a new sort-freeze window.
        """
        if self._selected_row_key and self._selected_row_key in table.rows:
            selected_row_index = table.get_row_index(self._selected_row_key)
            if table.cursor_row != selected_row_index:
                self._restoring_cursor = True
                try:
                    table.move_cursor(
                        row=selected_row_index, animate=False, scroll=False
                    )
                finally:
                    self._restoring_cursor = False

    def _should_bail_clf(self) -> bool:
        """
        Decide whether we have enough evidence to abort with a CLF hint.

        We require both a meaningful total of input lines and a clear majority
        of Apache-CLF-shaped lines among them, so a tiny burst of unrelated
        text or a single malformed line does not push the user out of the UI.
        """
        with self.stats.lock:
            return (
                self.stats.parsed_lines == 0
                and self.stats.total_lines >= _CLF_BAIL_MIN_TOTAL
                and self.stats.unsupported_clf_lines >= _CLF_BAIL_MIN_DETECTED
            )

    def _tick(self) -> None:
        """
        Synchronize the rendered table with the current aggregate snapshot.

        Each tick takes a locked snapshot of the buckets, computes the desired
        rendered rows, and then applies only the minimal set of UI changes:
        changed cells are updated in place, missing rows are removed, new rows
        are added, and sorting is only rerun when needed.

        Row highlighting is restored by row key rather than by screen position so
        the same logical endpoint stays selected across re-sorts. The temporary
        sort freeze prevents that restoration from fighting the user's current
        interaction. If the subprocess has already exited, the app simply quits.
        """
        if self.proc.poll() is not None:
            self.exit()
            return
        if self._should_bail_clf():
            self.exit_error = _CLF_ERROR_MESSAGE
            self.exit()
            return
        table = self.query_one(DataTable)
        if len(table.columns) != len(_SORT_KEYS):
            return
        items, elapsed, total, parsed = self._snapshot_items()
        path_width = self._path_width()
        live_row_keys, rows_changed = self._apply_row_updates(
            table, items, path_width=path_width
        )
        rows_changed = self._prune_missing_rows(table, live_row_keys) or rows_changed
        self._maybe_sort_rows(table, rows_changed=rows_changed)
        self._restore_selected_row(table)
        self._last_path_width = path_width
        self.title = (
            f"apppack-stats for {self.app_name} — "
            f"{parsed}/{total} lines, {elapsed:.0f}s"
        )


def _shutdown_apppack_proc(proc: subprocess.Popen) -> bool:
    """
    Tear down the spawned ``apppack`` subprocess and report who initiated it.

    A live subprocess at this point means the user quit the UI while ``apppack``
    was still running, so we send it ``SIGTERM``. If it had already exited on
    its own, we leave the return code intact for the caller to interpret.
    Returns ``True`` when the shutdown was driven by us, ``False`` when the
    subprocess died on its own.
    """
    user_initiated = proc.poll() is None
    if user_initiated:
        proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    return user_initiated


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse command-line arguments for the ``apppack-stats`` CLI.

    The CLI is intentionally small and geared toward interactive use. Most
    options shape how much history is seeded into the live view, how often the
    UI refreshes, and whether the final aggregate snapshot should be exported as
    CSV when the session ends.
    """
    parser = argparse.ArgumentParser(
        prog="apppack-stats",
        description="Live response-time stats from an AppPack web service.",
    )
    parser.add_argument("app", help="AppPack app name")
    parser.add_argument(
        "--refresh",
        type=float,
        default=1.0,
        help="seconds between redraws (default: 1.0)",
    )
    parser.add_argument(
        "--start",
        default="5m",
        help="how far back to seed history, e.g. 30m, 2h, 1d (default: 5m)",
    )
    parser.add_argument(
        "--prefix",
        default=None,
        help=(
            "log group prefix filter passed to `apppack logs --prefix`. "
            "By default no filter is applied — non-access-log lines are "
            "skipped by the parser anyway."
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        default=None,
        help=(
            "write the final stats to PATH as CSV when the app exits. "
            "Use '-' for stdout."
        ),
    )
    parser.add_argument(
        "--no-normalize",
        dest="normalize",
        action="store_false",
        help="disable URL normalization (group by raw path)",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """
    Run the CLI end to end and return a process-style exit code.

    This function validates that the external ``apppack`` CLI is available,
    starts the streaming subprocess, launches the background reader thread, and
    hands control to Textual until the session ends.

    Shutdown is intentionally defensive. When the user quits the UI, the spawned
    ``apppack`` process is treated as ours to terminate, and its signal-noise
    stderr output is suppressed. If the subprocess dies on its own with a
    non-zero status, any captured stderr is surfaced because that is more likely
    to reflect a real failure.
    """
    args = _parse_args(argv)

    if shutil.which("apppack") is None:
        sys.stderr.write(
            "error: 'apppack' CLI not found on PATH.\n"
            "Install it from https://docs.apppack.io/how-to/install-the-cli/\n"
        )
        return 2

    cmd = [
        "apppack",
        "logs",
        "-a",
        args.app,
        "--follow",
        "--raw",
        "--start",
        args.start,
    ]
    if args.prefix:
        cmd += ["--prefix", args.prefix]

    proc = subprocess.Popen(  # noqa: S603
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        # Detach apppack from our session so it has no controlling
        # terminal. Otherwise it writes "Keyboard interrupt detected,
        # exiting..." to /dev/tty on shutdown, bypassing our stderr
        # pipe and bleeding into the user's terminal after we quit.
        start_new_session=True,
    )
    if proc.stdout is None:
        sys.stderr.write("error: failed to capture apppack stdout.\n")
        return 2

    stats = Stats(normalize=args.normalize)
    stop = threading.Event()
    reader = threading.Thread(
        target=_reader_thread, args=(stats, proc.stdout, stop), daemon=True
    )
    reader.start()

    app = StatsApp(stats, app_name=args.app, refresh=args.refresh, proc=proc)
    try:
        app.run()
    finally:
        stop.set()
        user_initiated = _shutdown_apppack_proc(proc)

    if args.output:
        _write_csv(
            stats,
            args.output,
            sort_col=app.sort_col,
            reverse=app.sort_reverse,
        )
        if args.output != "-":
            pass

    if app.exit_error:
        sys.stderr.write(app.exit_error)
        return 2

    if not user_initiated and proc.returncode != 0:
        stderr = proc.stderr.read() if proc.stderr else ""
        if stderr:
            sys.stderr.write(stderr)
        return proc.returncode
    return 0


def _write_csv(stats: Stats, path: str, *, sort_col: str, reverse: bool) -> None:
    """
    Write the current aggregate snapshot in the same order as the live table.

    Export happens after the interactive session ends, so this function takes a
    one-time locked snapshot of the buckets and sorts it using the final UI sort
    state. Writing to ``"-"`` targets standard output; any other path is opened
    as a normal CSV file and overwritten.
    """
    with stats.lock:
        items = list(stats.buckets.items())
    items.sort(key=_SORT_KEYS[sort_col], reverse=reverse)

    def emit(out: IO[str]) -> None:
        writer = csv.writer(out)
        writer.writerow(
            [
                "method",
                "path",
                "count",
                "avg_ms",
                "p95_ms",
                "max_ms",
                "errors_4xx",
                "errors_5xx",
            ]
        )
        for (method, p), bucket in items:
            writer.writerow(
                [
                    method,
                    p,
                    bucket.count,
                    f"{bucket.avg_ms:.1f}",
                    f"{bucket.p95_ms:.1f}",
                    f"{bucket.max_ms:.1f}",
                    bucket.errors_4xx,
                    bucket.errors_5xx,
                ]
            )

    if path == "-":
        emit(sys.stdout)
    else:
        with Path(path).open("w", newline="") as f:
            emit(f)


if __name__ == "__main__":
    raise SystemExit(main())
