"""Live response-time stats from streamed AppPack web logs.

Spawns ``apppack logs --raw --follow`` for the requested app and aggregates
incoming requests per ``(method, normalized path)`` into a sortable Textual
data table. Click any column header to sort by it; click again to reverse.
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
from time import monotonic
from typing import IO

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header

from apppack_stats.extractors import extract_request

__version__ = "0.3.0"

# Order matters: UUIDs and hex hashes before bare ints.
_UUID_RE = re.compile(
    r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?=/|$)", re.I
)
_HEX_RE = re.compile(r"/[0-9a-f]{16,}(?=/|$)", re.I)
_INT_RE = re.compile(r"/\d+(?=/|$)")


def normalize_path(path: str) -> str:
    path = _UUID_RE.sub("/<uuid>", path)
    path = _HEX_RE.sub("/<hash>", path)
    path = _INT_RE.sub("/<id>", path)
    return path


@dataclass
class Bucket:
    count: int = 0
    errors: int = 0
    times_us: list[int] = field(default_factory=list)

    def add(self, time_us: int, status: int) -> None:
        self.count += 1
        self.times_us.append(time_us)
        if status >= 400:
            self.errors += 1

    @property
    def avg_ms(self) -> float:
        return statistics.fmean(self.times_us) / 1000

    @property
    def p95_ms(self) -> float:
        if len(self.times_us) < 2:
            return self.times_us[0] / 1000
        return statistics.quantiles(self.times_us, n=20)[18] / 1000

    @property
    def max_ms(self) -> float:
        return max(self.times_us) / 1000


class Stats:
    def __init__(self, *, normalize: bool) -> None:
        self.normalize = normalize
        self.buckets: dict[tuple[str, str], Bucket] = defaultdict(Bucket)
        self.total_lines = 0
        self.parsed_lines = 0
        self.started = monotonic()
        self.lock = threading.Lock()

    def ingest(self, line: str) -> None:
        self.total_lines += 1
        line = line.strip()
        if not line or line[0] != "{":
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

# Column key -> sort key over (key, bucket) tuples.
_SORT_KEYS: dict[str, "callable"] = {
    "method": lambda kv: kv[0][0],
    "path": lambda kv: kv[0][1],
    "count": lambda kv: kv[1].count,
    "avg": lambda kv: kv[1].avg_ms,
    "p95": lambda kv: kv[1].p95_ms,
    "max": lambda kv: kv[1].max_ms,
    "err": lambda kv: kv[1].errors,
}

_NUMERIC_COLS = {"count", "avg", "p95", "max", "err"}


def _reader_thread(stats: Stats, stream: IO[str], stop: threading.Event) -> None:
    for line in stream:
        if stop.is_set():
            break
        stats.ingest(line)
    stop.set()


class StatsApp(App):
    """Textual UI for live response-time stats."""

    CSS = """
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("n", "toggle_normalize", "Toggle path normalization"),
    ]

    def __init__(
        self,
        stats: Stats,
        *,
        refresh: float,
        proc: subprocess.Popen,
    ) -> None:
        super().__init__()
        self.stats = stats
        self.refresh_sec = refresh
        self.proc = proc
        self.sort_col = "avg"
        self.sort_reverse = True

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(zebra_stripes=True, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Method", key="method", width=8)
        table.add_column("Path", key="path")
        table.add_column("Count", key="count", width=8)
        table.add_column("Avg ms", key="avg", width=8)
        table.add_column("p95 ms", key="p95", width=8)
        table.add_column("Max ms", key="max", width=8)
        table.add_column("Err", key="err", width=6)
        self.title = "apppack-stats"
        self._update_subtitle()
        self.set_interval(self.refresh_sec, self._tick)
        self._tick()

    def on_data_table_header_selected(
        self, event: DataTable.HeaderSelected
    ) -> None:
        col_key = str(event.column_key.value) if event.column_key else ""
        if col_key not in _SORT_KEYS:
            return
        if col_key == self.sort_col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = col_key
            self.sort_reverse = col_key in _NUMERIC_COLS
        self._update_subtitle()
        self._tick()

    def action_toggle_normalize(self) -> None:
        with self.stats.lock:
            self.stats.normalize = not self.stats.normalize
        self._update_subtitle()

    def _update_subtitle(self) -> None:
        arrow = "↓" if self.sort_reverse else "↑"
        norm = "" if self.stats.normalize else "  (raw paths)"
        self.sub_title = f"sort: {self.sort_col} {arrow}{norm}"

    def _tick(self) -> None:
        if self.proc.poll() is not None:
            self.exit()
            return
        with self.stats.lock:
            items = list(self.stats.buckets.items())
            elapsed = monotonic() - self.stats.started
            total = self.stats.total_lines
            parsed = self.stats.parsed_lines
        items.sort(key=_SORT_KEYS[self.sort_col], reverse=self.sort_reverse)

        table = self.query_one(DataTable)
        table.clear()
        for (method, path), bucket in items:
            table.add_row(
                method,
                path,
                str(bucket.count),
                f"{bucket.avg_ms:.0f}",
                f"{bucket.p95_ms:.0f}",
                f"{bucket.max_ms:.0f}",
                str(bucket.errors) if bucket.errors else "",
            )
        self.title = f"apppack-stats — {parsed}/{total} lines, {elapsed:.0f}s"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
    args = _parse_args(argv)

    if shutil.which("apppack") is None:
        sys.stderr.write(
            "error: 'apppack' CLI not found on PATH.\n"
            "Install it from https://docs.apppack.io/how-to/install-the-cli/\n"
        )
        return 2

    cmd = [
        "apppack", "logs",
        "-a", args.app,
        "--follow",
        "--raw",
        "--start", args.start,
    ]
    if args.prefix:
        cmd += ["--prefix", args.prefix]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None

    stats = Stats(normalize=args.normalize)
    stop = threading.Event()
    reader = threading.Thread(
        target=_reader_thread, args=(stats, proc.stdout, stop), daemon=True
    )
    reader.start()

    app = StatsApp(stats, refresh=args.refresh, proc=proc)
    try:
        app.run()
    finally:
        stop.set()
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    if args.output:
        _write_csv(
            stats,
            args.output,
            sort_col=app.sort_col,
            reverse=app.sort_reverse,
        )

    rc = proc.returncode or 0
    if rc not in (0, -15):
        stderr = proc.stderr.read() if proc.stderr else ""
        if stderr:
            sys.stderr.write(stderr)
        return rc
    return 0


def _write_csv(
    stats: Stats, path: str, *, sort_col: str, reverse: bool
) -> None:
    with stats.lock:
        items = list(stats.buckets.items())
    items.sort(key=_SORT_KEYS[sort_col], reverse=reverse)

    def emit(out: IO[str]) -> None:
        writer = csv.writer(out)
        writer.writerow(
            ["method", "path", "count", "avg_ms", "p95_ms", "max_ms", "errors"]
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
                    bucket.errors,
                ]
            )

    if path == "-":
        emit(sys.stdout)
    else:
        with open(path, "w", newline="") as f:
            emit(f)


if __name__ == "__main__":
    raise SystemExit(main())
