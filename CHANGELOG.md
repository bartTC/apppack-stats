# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Detect Apache/NCSA Common and Combined Log Format access logs and
  exit with a clear configuration hint pointing at the supported JSON
  shapes. These plain-text formats have no request-time field, so the
  app now stops fast instead of staying empty forever.

## [0.5.0] - 2026-05-07

### Added

- Press `c` to copy the highlighted row's full path to the system
  clipboard. Uses `pbcopy` on macOS and `wl-copy` / `xclip` / `xsel`
  on Linux, with Textual's OSC 52 path as a fallback for SSH sessions.

## [0.4.0] - 2026-05-07

### Changed

- The title bar now includes the target app name as
  `apppack-stats for <app>`, and keeps that prefix while the live
  counters update.

### Fixed

- The DataTable now follows Textual's intended live-update path:
  rows are updated in place with stable row keys, removed with
  `remove_row`, and re-sorted without clearing and rebuilding the
  widget on every refresh tick. Only changed rows are touched, which
  makes scrolling much smoother on busy tables. Row highlighting is
  preserved by row key across refreshes and re-sorts, so a selected
  endpoint stays selected even while new data arrives.

## [0.3.0] - 2026-05-07

### Added

- Support for the gunicorn structlog access-log shape: top-level
  `request_method` / `request_path` / `response_status` / `response_time`
  (seconds, float). The original AppPack default shape (`method` /
  `path` / `status` / `response_time_us`) continues to work; the parser
  tries both.
- `-o` / `--output PATH` flag: when supplied, the final aggregated
  stats are written to PATH as CSV after the app exits, sorted by
  whichever column was active in the UI. Pass `-` to write to stdout.

### Changed

- The single "Err" column has been split into separate "4xx" and "5xx"
  columns (and `errors_4xx` / `errors_5xx` in the CSV output). 4xx
  responses are usually noise (404 on `/favicon.ico`, 401s, etc.);
  5xx responses are the signal you actually want to surface.
- Long request paths are now truncated with an ellipsis so the
  numeric columns are always visible, even on narrow terminals. The
  truncation width adapts to the terminal width on resize.

### Fixed

- `apppack` no longer leaks its "Keyboard interrupt detected,
  exiting..." message to the user's terminal on quit. We spawn it in
  a new session (no controlling TTY) and, when *we* are the ones
  shutting it down, swallow its captured stderr — any signal-handler
  noise it emits is a side effect of our signal, not a real error.

### Removed

- The static rich summary table that was printed after the Textual app
  exited. `rich` is no longer a runtime dependency.

## [0.2.0] - 2026-05-07

### Changed

- Replaced the `rich.live.Live` renderer with a Textual `DataTable`.
  Click any column header to sort by it; click again to flip direction.
- The table now fills the terminal and scrolls vertically; the `--top`
  cap has been removed.
- Added keybindings: `q` to quit, `n` to toggle URL normalization at
  runtime. The active sort and normalization state are shown in the
  header subtitle.
- `--prefix` is no longer defaulted to `web`. AppPack's `--prefix`
  filter matches the underlying CloudWatch log-group name, which does
  not always start with the service label shown in the rendered
  `(web/web/<task>)` prefix. Passing a default of `web` silently
  dropped every line for apps with differently-named log groups.
  Pass `--prefix` explicitly if you want to scope to one service.

### Fixed

- Apps whose CloudWatch log-group names don't begin with `web` no
  longer parse zero lines.

## [0.1.0] - 2026-05-07

### Added

- Initial release. Transformed the original `log.py` stdin-piping uv
  script into a packaged CLI publishable to PyPI and runnable as
  `uvx apppack-stats <appname>`.
- The CLI now spawns `apppack logs --raw --follow` itself, so no
  shell piping is required.
- Aggregates incoming requests per `(method, normalized path)` into
  per-bucket count, average, p95, and max response times, plus 4xx/5xx
  error counts.
- URL normalization for UUIDs (`/<uuid>`), long hex hashes
  (`/<hash>`), and integer IDs (`/<id>`); disable with `--no-normalize`.
- Live `rich` table refresh, with a final summary printed on exit.
- Pre-flight check that the `apppack` CLI is on `PATH`, with a friendly
  install pointer.
- Hatchling build, MIT licence, README, `.gitignore`.
