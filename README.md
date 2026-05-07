# apppack-stats

Live response-time stats from streamed [AppPack](https://apppack.io) web logs.

`apppack-stats` shells out to `apppack logs --raw --follow` for the app you
name, parses the JSON request lines, and renders a live [Textual] data
table grouped by `(method, normalized path)`. Click any column header to
re-sort by it; click again to flip the direction.

[Textual]: https://textual.textualize.io/

```
Slowest endpoints — 1842/1903 lines parsed, 47s elapsed
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━┓
┃ Method ┃ Path                          ┃ Count ┃ Avg ms ┃ p95 ms ┃ Max ms ┃ Err ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━┩
│ GET    │ /reports/<id>/export          │     7 │   1842 │   2210 │   2480 │     │
│ POST   │ /api/v1/orders                │    23 │    412 │    711 │    980 │   2 │
│ GET    │ /dashboard                    │    91 │    188 │    320 │    640 │     │
└────────┴───────────────────────────────┴───────┴────────┴────────┴────────┴─────┘
```

## Usage

No install required — run it straight from PyPI with
[`uv`](https://docs.astral.sh/uv/):

```sh
uvx apppack-stats <appname>
```

Press `q` (or `Ctrl+C`) to stop.

### Keys

| Key                 | Action                                       |
| ------------------- | -------------------------------------------- |
| Click column header | Sort by that column (click again to reverse) |
| `n`                 | Toggle URL normalization on/off              |
| `q`                 | Quit                                         |

The [AppPack CLI](https://docs.apppack.io/how-to/install-the-cli/) must be on
your `PATH` and authenticated against the account that owns the app.

## Options

| Flag              | Default | Notes                                               |
| ----------------- | ------- | --------------------------------------------------- |
| `--refresh SEC`   | `1.0`   | Seconds between redraws                             |
| `--start DUR`     | `5m`    | How far back to seed history (`30m`, `2h`, `1d`, …) |
| `--prefix STRING` | _none_  | AppPack log-group prefix filter — see note below    |
| `--no-normalize`  | off     | Group by raw path instead of normalizing IDs        |
| `-o`, `--output PATH` | _none_ | On exit, write the final stats to PATH as CSV (`-` for stdout). Rows are sorted by whichever column was active in the UI when you quit. |

The table fills the terminal and scrolls when there are more rows than fit.

### A note on `--prefix`

`apppack logs --prefix` filters on AppPack's underlying CloudWatch
log-group names, which don't always begin with the service name shown in
the rendered `(web/web/<task>)` label. Different AppPack apps use
different log-group naming conventions, so a fixed default like `web`
silently drops everything for some apps. By default `apppack-stats`
passes no prefix and lets the parser filter — only access-log JSON lines
(those with `method`, `path`, `status`, `response_time_us`) are counted;
everything else is skipped.

## URL normalization

By default, paths are normalized so that `/orders/12345` and `/orders/67890`
share a row as `/orders/<id>`. UUIDs become `/<uuid>`, long hex hashes
become `/<hash>`. Pass `--no-normalize` to keep raw paths.

## Adding support for another log shape

Access-log shapes live in
[`src/apppack_stats/extractors.py`](src/apppack_stats/extractors.py).
Append a new `LogShape(...)` to the `SHAPES` list — pick a `time_field`
key that doesn't collide with existing ones, set `time_unit` to `"us"`,
`"ms"`, or `"s"`, and the parser will pick it up automatically.
