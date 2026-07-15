# Changelog

## 0.1.0 (unreleased)

Initial release, extracted from an internal BlenderKit prototype:

- API endpoints (`api_data/older/output/merges`) now return a JSON **403**
  when the staff session is missing/expired, instead of a 302 redirect to the
  HTML admin-login page (which made `fetch().json()` fail with a cryptic
  "failed to load"). The dashboard detects the 403 and shows a clear
  "session expired — reload to log in" banner.
- Timeline: you can now pan/zoom freely to the present (even when the newest
  run is hours old) and zoom out to the whole loaded span; panning to the
  right edge pulls current data, panning to the left loads older history.
- Refresh now "live-follows": if you're watching the live edge it snaps to the
  freshly-fetched present so new runs are visible; if you've panned into the
  past it leaves your view put.
- Example project: a demo auto-login middleware keeps the demo staff user
  signed in on every request, so reloading a deep link no longer bounces to
  the admin login. 
- Optional persistent history: a `Run` model + `sync_scheduler_runs`
  management command (idempotent upsert); persisted periods are served
  from the database (instant, beyond drain retention). Runs only — job
  output and slow queries stay on-demand.

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
