from __future__ import annotations

import asyncio

from textual.widgets import DataTable

from apppack_stats import Stats, StatsApp


class DummyProc:
    def poll(self) -> None:
        return None


def test_stats_app_keeps_selected_endpoint_highlighted_across_resort() -> None:
    async def run() -> None:
        stats = Stats(normalize=True)
        for i in range(8):
            bucket = stats.buckets[("GET", f"/path/{i}")]
            bucket.count = i + 1
            bucket.times_us = [1000 + i]

        app = StatsApp(
            stats,
            app_name="demo-app",
            refresh=1.0,
            proc=DummyProc(),
        )

        async with app.run_test(size=(100, 20)):
            app._tick()
            table = app.query_one(DataTable)
            target_row_key = app._row_key("GET", "/path/4")
            app._selected_row_key = target_row_key
            table.move_cursor(row=table.get_row_index(target_row_key))

            stats.buckets[("GET", "/path/7")].times_us = [1]
            app._needs_sort = True
            app._sort_frozen_until = 0.0
            app._tick()

            assert table.rows
            assert app._selected_row_key == target_row_key
            assert table.cursor_row == table.get_row_index(target_row_key)

    asyncio.run(run())
