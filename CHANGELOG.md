# Changelog

## 0.3.1

- `sync_scheduler_runs` survives unstorable runs: each row is upserted
  individually, failures are logged with full tracebacks and summarised on
  stderr instead of aborting the whole sync. (A single one-off dyno with a
  ~2000-char base64-wrapped shell command produced a label past
  `varchar(500)`; the resulting `DataError` killed every sync and the
  dashboard recorded nothing for days.)
- `Run.upsert_from_dict` clamps every sized field to its column's
  `max_length`, so oversized log-derived labels store truncated instead of
  failing. `command` is a TextField and keeps the full command line.

## 0.3.0

- Chained (`&&`) commands are labeled by every segment: label rules apply per
  segment (one matching segment no longer swallows the whole chain), repeated
  segment names collapse, and args are dropped for a stable lane name.
  Previously such a run was named by whichever rule or first `manage.py`
  subcommand matched the combined string, hiding the other jobs in the chain.

## 0.2.0

- Hardening: every response now carries `Cache-Control: no-store`, so the
  production log data it serves is never cached by a browser or a shared CDN.
- New `REQUIRE_VERIFIED` setting (default `False`): when enabled, access also
  requires a 2FA-verified session (django-otp's `request.user.is_verified()`),
  on top of staff status. Degrades to staff-only when django-otp isn't present.

## 0.1.0

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
