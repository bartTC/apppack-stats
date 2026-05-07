# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
