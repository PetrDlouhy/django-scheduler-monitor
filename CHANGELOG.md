# Changelog

## 0.1.0 (unreleased)

Initial release, extracted from an internal BlenderKit prototype:

- Timeline dashboard of all Heroku one-off dyno runs (Scheduler,
  Advanced Scheduler, `heroku run`) reconstructed from the log drain.
- Per-run memory (RSS) and load-average charts, outcome classification
  (success / failed / OOM-killed / timed-out at the 24h wall / stopped via
  `ps:stop` / crashed / running).
- Combined chronological stream of job output + attributed slow SQL queries
  with click-to-mark on the charts.
- Lane grouping: automatic numeric-argument normalization + manual
  merge/split overrides stored in the database.
- Filters (outcome, cadence, source, job search, memory pressure,
  downscale candidates), sortable lanes, pan-to-load-older history.
- Demo mode with generated data for trying the dashboard without any
  log-drain setup.
