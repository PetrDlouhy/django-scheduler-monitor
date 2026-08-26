"""Unit tests for the log-line parsing / run-reconstruction core."""

from datetime import datetime, timedelta, timezone

from scheduler_monitor import core

T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def ts(offset_s: float) -> str:
    return (T0 + timedelta(seconds=offset_s)).isoformat()


def line(offset_s, procid, message):
    return {"_time": ts(offset_s), "procid": procid, "message": message}


def start_line(offset_s, procid, command):
    return line(offset_s, procid, f"Starting process with command `{command}`")


class TestJobKey:
    def test_manage_py_command(self):
        assert core.job_key("poetry run python manage.py rollup --days 90") == (
            "rollup", "--days 90")

    def test_label_rules_take_precedence(self):
        key, args = core.job_key('curl -o /dev/null "https://x?renew_cache=1"',
                                 [(r"renew_cache", "curl: cache warm")])
        assert (key, args) == ("curl: cache warm", "")

    def test_plain_command_uses_first_token(self):
        assert core.job_key("bash")[0] == "bash"

    def test_chained_commands_name_every_segment(self):
        cmd = ("poetry run python manage.py update_contacts && "
               "poetry run python manage.py sendgrid_unsubscribe_users && "
               "poetry run python manage.py assign_guessed_countries 7")
        assert core.job_key(cmd) == (
            "update_contacts && sendgrid_unsubscribe_users && assign_guessed_countries", "")

    def test_chained_commands_apply_label_rules_per_segment(self):
        cmd = ('poetry run python manage.py clean_timed_caches && '
               'curl -sf -o /dev/null "https://x/a?renew_cache=1" && '
               'curl -sf -o /dev/null "https://x/b?renew_cache=1"')
        assert core.job_key(cmd, [(r"^curl\b", "curl: cache warm")]) == (
            "clean_timed_caches && curl: cache warm", "")


class TestClassify:
    def test_exit_codes(self):
        assert core.classify(0, False, True) == "success"
        assert core.classify(137, False, True) == "oom_killed"
        assert core.classify(143, False, True) == "timed_out"
        assert core.classify(1, False, True) == "failed"

    def test_complete_without_exit_line_is_success(self):
        # Heroku "complete" means clean termination; the drain just dropped the exit line.
        assert core.classify(None, False, True) == "success"

    def test_no_terminal_is_running(self):
        assert core.classify(None, False, False) == "running"


class TestBuildRuns:
    def test_basic_run(self):
        runs = core.build_runs([
            start_line(0, "scheduler.1", "python manage.py do_it --days 90"),
            line(1, "scheduler.1", "State changed from starting to up"),
            line(60, "scheduler.1", "Process exited with status 0"),
            line(61, "scheduler.1", "State changed from up to complete"),
        ])
        assert len(runs) == 1
        r = runs[0]
        assert r["job_label"] == "do_it --days 90"
        assert r["group_label"] == "do_it --days N"  # numeric args normalized
        assert r["outcome"] == "success"
        assert 58 < r["duration_s"] < 62

    def test_recycled_dyno_name_splits_into_two_runs(self):
        rows = []
        for base in (0, 3600):  # Heroku reuses scheduler.NNNN across runs
            rows += [
                start_line(base, "scheduler.7", "python manage.py a"),
                line(base + 1, "scheduler.7", "State changed from starting to up"),
                line(base + 30, "scheduler.7", "Process exited with status 0"),
                line(base + 31, "scheduler.7", "State changed from up to complete"),
            ]
        assert len(core.build_runs(rows)) == 2

    def test_fast_job_skipping_up_state_still_closes(self):
        runs = core.build_runs([
            start_line(0, "scheduler.2", "python manage.py quick"),
            line(1, "scheduler.2", "State changed from starting to complete"),
        ])
        assert runs[0]["outcome"] == "success"

    def test_manual_run_command_backfilled_from_api_announcement(self):
        # API/manual `run.` dynos never log their own Starting line; the command
        # is announced by the `api` control plane.
        runs = core.build_runs([
            line(0, "api", "Starting process with command `python manage.py manual_job` by user x@y"),
            line(5, "run.42", "State changed from starting to up"),
            line(90, "run.42", "Process exited with status 0"),
            line(91, "run.42", "State changed from up to complete"),
        ])
        assert len(runs) == 1
        assert runs[0]["job_label"] == "manual_job"
        assert runs[0]["source"] == "Manual run"

    def test_scheduler_announcement_not_stolen_by_manual_run(self):
        # The scheduler dyno logs its own command AND `api` announces it; the
        # manual run must pick up the remaining announcement, not the duplicate.
        runs = core.build_runs([
            line(0, "api", "Starting process with command `python manage.py sched_job`"),
            line(1, "api", "Starting process with command `python manage.py manual_job` by user x@y"),
            start_line(2, "scheduler.9", "python manage.py sched_job"),
            line(3, "scheduler.9", "State changed from starting to up"),
            line(6, "run.10", "State changed from starting to up"),
        ])
        by_dyno = {r["dyno"]: r for r in runs}
        assert by_dyno["run.10"]["job_label"] == "manual_job"

    def test_ps_stop_marker_attaches_even_after_complete(self):
        # Heroku logs "up to complete" BEFORE "Stopping ... SIGTERM" on ps:stop.
        runs = core.build_runs([
            start_line(0, "scheduler.3", "python manage.py long_job"),
            line(1, "scheduler.3", "State changed from starting to up"),
            line(100, "scheduler.3", "State changed from up to complete"),
            line(101, "scheduler.3", "Stopping all processes with SIGTERM"),
            line(105, "scheduler.3", "Process exited with status 0"),
        ])
        core.mark_stopped(runs)
        assert runs[0]["outcome"] == "stopped"

    def test_cycling_sigterm_is_not_marked_stopped(self):
        runs = core.build_runs([
            start_line(0, "scheduler.4", "python manage.py long_job"),
            line(1, "scheduler.4", "State changed from starting to up"),
            line(90, "scheduler.4", "Cycling"),
            line(91, "scheduler.4", "Stopping all processes with SIGTERM"),
            line(95, "scheduler.4", "State changed from up to complete"),
        ])
        core.mark_stopped(runs)
        assert runs[0]["outcome"] != "stopped"


class TestOutcomeFixups:
    def _run(self, **kw):
        base = {"dyno": "scheduler.1", "job_label": "j", "group_label": "j",
                "cadence": "daily", "start": ts(0), "end": ts(1), "duration_s": 1,
                "exit_status": None, "outcome": "success",
                "mem_series": None, "load_series": None}
        base.update(kw)
        return base

    def test_24h_wall_reclassified_as_timed_out(self):
        r = self._run(duration_s=24 * 3600, exit_status=None)
        core.mark_24h_kills([r])
        assert r["outcome"] == "timed_out"

    def test_clean_exit_just_under_the_wall_stays_success(self):
        r = self._run(duration_s=24 * 3600, exit_status=0)
        core.mark_24h_kills([r])
        assert r["outcome"] == "success"

    def test_open_run_with_fresh_telemetry_stays_running(self):
        now = T0 + timedelta(seconds=3600)
        r = self._run(end=None, duration_s=None, outcome="running",
                      mem_series=[(3550.0, 100.0)])
        core.close_open_runs([r], now)
        assert r["end"] is None and r["outcome"] == "running"

    def test_open_run_with_stale_telemetry_gets_closed(self):
        now = T0 + timedelta(seconds=7200)
        r = self._run(end=None, duration_s=None, outcome="running",
                      mem_series=[(1000.0, 100.0)])
        core.close_open_runs([r], now)
        assert r["end"] is not None and r["outcome"] == "ended"


class TestInferCadence:
    def test_variants_pool_starts_via_group_label(self):
        runs = []
        for day in range(4):  # alternating args, same normalized group => daily
            args = "--days 90" if day % 2 else "--days 365"
            runs.append({"group_label": "rollup --days N",
                         "job_label": f"rollup {args}",
                         "start": ts(day * 86400)})
        assert core.infer_cadence(runs)["rollup --days N"] == "daily"


class TestSlowQueries:
    def test_reassembles_split_lines_and_keeps_only_dyno_attributed(self, settings):
        settings.SCHEDULER_MONITOR = {"AXIOM_DATASET": "x"}
        rows = [
            # Heroku splits lines at byte boundaries (mid-word); parts arrive unordered
            {"mint": ts(10), "dur": 2500.0, "raw": [
                '2|SEP|[CHARCOAL] [11-2]  OM "t" WHERE "t"."id" = 5',
                '1|SEP|[CHARCOAL] [11-1] application_name="scheduler.12" '
                "LOG:  duration: 2500.0 ms  statement: SELECT * FR",
            ]},
            {"mint": ts(11), "dur": 3000.0, "raw": [
                '1|SEP|[PUCE] [3-1] application_name="[unknown]" '
                "LOG:  duration: 3000.0 ms  statement: SELECT 1",
            ]},
        ]
        out = core.reassemble_slow_queries(rows)
        assert len(out) == 1  # the [unknown] one is dropped — no guessing
        assert out[0]["app"] == "scheduler.12"
        assert out[0]["sql"] == 'SELECT * FROM "t" WHERE "t"."id" = 5'
