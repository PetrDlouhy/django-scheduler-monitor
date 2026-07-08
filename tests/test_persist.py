"""Persistence layer: the Run model + sync command + DB-first older-history."""

import json

import pytest
from django.core.management import call_command
from django.urls import reverse

from scheduler_monitor.models import SERIES_CAP, Run, _downsample


@pytest.fixture
def staff_client(client, db):
    from django.contrib.auth.models import User
    client.force_login(User.objects.create_user("ops", password="x", is_staff=True))
    return client


def test_sync_command_persists(db):
    call_command("sync_scheduler_runs")  # DEMO mode -> generated dataset
    assert Run.objects.count() > 50


def test_upsert_is_idempotent_and_updates(db):
    r = {"dyno": "scheduler.1", "start": "2026-07-01T12:00:00+00:00", "end": None,
         "source": "Scheduler", "command": "python manage.py x", "job_key": "x",
         "job_label": "x", "group_label": "x", "args": "", "duration_s": None,
         "exit_status": None, "outcome": "running", "stopped": False, "cycled": False,
         "peak_rss_mb": None, "quota_mb": None, "mem_pct": None, "peak_load": None,
         "dyno_size": None, "cadence": "daily", "mem_series": None, "load_series": None}
    assert Run.upsert_from_dict(r) is True
    # a later sync sees the same run finished -> same row updated, no duplicate
    done = {**r, "end": "2026-07-01T12:05:00+00:00", "duration_s": 300.0,
            "exit_status": 0, "outcome": "success"}
    assert Run.upsert_from_dict(done) is False
    assert Run.objects.count() == 1
    assert Run.objects.get().outcome == "success"


def test_run_dict_roundtrip(db):
    call_command("sync_scheduler_runs")
    run = Run.objects.exclude(mem_series=None).exclude(end=None).first()
    d = run.to_run_dict()
    for field in ("dyno", "job_label", "group_label", "outcome", "cadence",
                  "peak_rss_mb", "quota_mb", "mem_series"):
        assert d[field] == getattr(run, field)
    # ISO strings the dashboard's Date.parse understands, with tz info
    assert "T" in d["start"] and ("+" in d["start"] or d["start"].endswith("Z"))


def test_series_capped_on_store():
    series = [(float(i), float(i % 97)) for i in range(5000)]
    capped = _downsample(series)
    assert len(capped) == SERIES_CAP
    assert max(v for _, v in capped) == max(v for _, v in series)  # peaks survive


def _open_run(start, mem_series=None, **kw):
    from datetime import datetime, timezone
    defaults = dict(dyno=kw.pop("dyno", "scheduler.1"), source="Scheduler",
                    command="python manage.py x", job_key="x", job_label="x",
                    group_label="x", args="", outcome="running",
                    mem_series=mem_series, end=None, duration_s=None)
    defaults.update(kw)
    if isinstance(start, str):
        start = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    return Run.objects.create(start=start, **defaults)


def test_close_stale_ends_old_running_row_at_last_telemetry(db):
    from datetime import timedelta

    from django.utils import timezone
    now = timezone.now()
    # persisted mid-run 3 days ago, last telemetry 2h in -> can't still be running
    _open_run(now - timedelta(days=3), mem_series=[[0.0, 100.0], [7200.0, 300.0]])
    assert Run.close_stale(now) == 1
    run = Run.objects.get()
    assert run.outcome == "ended"
    assert run.duration_s == 7200.0
    assert run.end == run.start + timedelta(seconds=7200)


def test_close_stale_marks_24h_runs_timed_out(db):
    from datetime import timedelta

    from django.utils import timezone
    now = timezone.now()
    _open_run(now - timedelta(days=2), mem_series=[[0.0, 10.0], [86000.0, 20.0]])
    Run.close_stale(now)
    assert Run.objects.get().outcome == "timed_out"


def test_close_stale_leaves_genuinely_running_rows_alone(db):
    from datetime import timedelta

    from django.utils import timezone
    now = timezone.now()
    _open_run(now - timedelta(hours=2), mem_series=[[0.0, 10.0]])
    assert Run.close_stale(now) == 0
    assert Run.objects.get().outcome == "running"


def test_close_stale_falls_back_to_sibling_median(db):
    from datetime import timedelta

    from django.utils import timezone
    now = timezone.now()
    for i, dur in enumerate([100.0, 200.0, 300.0]):  # finished siblings
        _open_run(now - timedelta(hours=30 + i), dyno=f"scheduler.{i}",
                  outcome="success", end=now, duration_s=dur)
    _open_run(now - timedelta(days=2), dyno="scheduler.99", mem_series=None)
    Run.close_stale(now)
    assert Run.objects.get(dyno="scheduler.99").duration_s == 200.0


def test_api_older_serves_from_db_when_persisted(staff_client):
    call_command("sync_scheduler_runs")
    newest = Run.objects.order_by("-start").first()
    resp = staff_client.get(reverse("scheduler_monitor:api_older"),
                            {"before": newest.start.isoformat()})
    data = resp.json()
    assert data["ok"] and data["source"] == "db"
    assert len(data["runs"]) > 0
    assert data["slow_queries"] == []  # deliberately not stored
    # wire format matches the live dataset's run dicts
    assert {"dyno", "start", "outcome", "group_label"} <= set(data["runs"][0])


def test_api_older_empty_db_falls_back(staff_client):
    # DEMO fallback returns an empty chunk (no Axiom in tests), but the point
    # is: no Run rows -> the DB branch is skipped without error.
    resp = staff_client.get(reverse("scheduler_monitor:api_older"),
                            {"before": "2026-01-01T00:00:00+00:00"})
    data = resp.json()
    assert data["ok"] and data["runs"] == []


def test_merge_overrides_survive_roundtrip_with_runs(staff_client):
    # unrelated tables coexist: overrides editing doesn't touch runs
    call_command("sync_scheduler_runs")
    n = Run.objects.count()
    staff_client.post(reverse("scheduler_monitor:api_merges"),
                      json.dumps({"merges": [], "splits": ["nightly_rollup --days N"]}),
                      content_type="application/json")
    assert Run.objects.count() == n
