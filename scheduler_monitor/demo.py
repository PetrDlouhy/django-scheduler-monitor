"""Demo dataset generator.

With ``SCHEDULER_MONITOR = {"DEMO": True}`` the dashboard serves generated data
instead of querying Axiom — for trying the app without any log-drain setup, and
for producing screenshots that contain no real production information.
"""

import random
from datetime import datetime, timedelta, timezone

# name, args, cadence_s, duration_s (mean), quota_mb, mem_profile, load_peak
DEMO_JOBS = [
    ("warm_caches", "", 600, 8, 512, "flat", 0.2),
    ("send_queued_emails", "--batch 500", 600, 25, 512, "flat", 0.4),
    ("generate_thumbnails", "", 600, 95, 1024, "saw", 0.8),
    ("sync_subscriptions", "--throttle=1", 3600, 340, 1024, "flat", 0.5),
    ("rebuild_search_index", "", 86400, 4100, 2560, "ramp", 2.1),
    ("nightly_rollup", "--days 90", 86400, 15500, 2560, "ramp", 0.6),
    ("nightly_rollup", "--days 365", 86400, 18200, 2560, "ramp", 0.7),
    ("cleanup_sessions", "", 86400, 45, 512, "flat", 0.3),
    ("db_backup", "--compress", 86400, 1900, 1024, "saw", 1.2),
    ("detect_abuse", "", 86400, 9800, 1024, "ramp", 0.9),
    ("recompute_stats", "--scope counts", 86400, 7200, 2560, "ramp", 1.4),
    ("recompute_stats", "--scope earnings", 86400, 11000, 2560, "ramp", 1.6),
]

DEMO_SQL = [
    'SELECT "app_asset"."id", "app_asset"."name", "app_asset"."score" FROM "app_asset" '
    'WHERE NOT ("app_asset"."status" = \'deleted\') ORDER BY "app_asset"."id" ASC',
    'SELECT "app_ledger"."currency", SUM("app_ledger"."amount") AS "total" FROM "app_ledger" '
    'WHERE ("app_ledger"."account_id" = 4021 AND "app_ledger"."id" <= 98021) GROUP BY 1',
    'SELECT COUNT(*) FROM (SELECT DISTINCT "app_usage"."user_id" AS "col1" FROM "app_usage" '
    'INNER JOIN "app_report" ON ("app_usage"."report_id" = "app_report"."id") '
    "WHERE \"app_report\".\"kind\" = 'download') subquery",
]


def _series(profile, duration_s, quota_mb, rng):
    pts, n = [], max(2, min(300, int(duration_s / 20)))
    base = quota_mb * rng.uniform(0.15, 0.3)
    for i in range(n):
        f = i / (n - 1)
        if profile == "ramp":
            v = base + (quota_mb * 0.55 - base) * f
        elif profile == "saw":
            v = base + quota_mb * 0.35 * abs((f * 6) % 2 - 1)
        else:
            v = base * rng.uniform(0.95, 1.1)
        pts.append((round(f * duration_s, 1), round(min(v, quota_mb * 0.97), 1)))
    return pts


def demo_dataset(lookback_days: float = 3.0) -> dict:
    now = datetime.now(timezone.utc)
    rng = random.Random(42)
    runs, slow_queries = [], []
    horizon = now - timedelta(days=lookback_days)

    for name, args, cad, dur_mean, quota, profile, load_peak in DEMO_JOBS:
        label = f"{name} {args}".strip()
        t = horizon + timedelta(seconds=rng.uniform(0, cad))
        seq = 1000
        while t < now:
            seq += rng.randint(3, 97)
            dur = max(2.0, rng.gauss(dur_mean, dur_mean * 0.15))
            end = t + timedelta(seconds=dur)
            outcome, exit_status = "success", 0
            roll = rng.random()
            if roll < 0.015:
                outcome, exit_status = "failed", 1
            elif roll < 0.025 and quota >= 1024:
                outcome, exit_status = "oom_killed", 137
            running = end > now
            if running:
                end, outcome, exit_status = None, "running", None
            mem = _series(profile, dur if not running else (now - t).total_seconds(),
                          quota, rng) if dur > 60 else None
            peak = max(p[1] for p in mem) if mem else round(quota * rng.uniform(0.2, 0.4), 1)
            dyno = f"scheduler.{seq}"
            runs.append({
                "dyno": dyno, "source": "Scheduler",
                "command": f"python manage.py {name} {args}".strip(),
                "job_key": name, "job_label": label,
                "group_label": label, "args": args,
                "start": t.isoformat(), "end": end.isoformat() if end else None,
                "duration_s": dur if not running else None,
                "exit_status": exit_status, "outcome": outcome,
                "stopped": False, "cycled": False,
                "peak_rss_mb": peak, "quota_mb": quota,
                "mem_pct": round(100 * peak / quota, 1),
                "peak_load": round(load_peak * rng.uniform(0.6, 1.1), 2),
                "dyno_size": {512: "Standard-1X", 1024: "Standard-2X",
                              2560: "Performance-M"}[quota],
                "mem_series": mem,
                "load_series": [(o, round(load_peak * rng.uniform(0.1, 1.0), 2))
                                for o, _ in (mem or [])] or None,
                "cadence": {600: "10 min", 3600: "hourly", 86400: "daily"}[cad],
            })
            if dur > 3600 and not running:  # attributed slow queries for long jobs
                for _ in range(rng.randint(2, 6)):
                    qt = t + timedelta(seconds=rng.uniform(60, dur - 60))
                    slow_queries.append({
                        "t": qt.isoformat(),
                        "dur_ms": round(rng.uniform(1500, 480000), 1),
                        "app": dyno, "sql": rng.choice(DEMO_SQL),
                    })
            t += timedelta(seconds=cad * rng.uniform(0.97, 1.03))

    # one manual run + one 24h-killed + one ps:stopped, for the full palette
    runs[-1]["outcome"] = "timed_out"
    runs[-1]["duration_s"] = 24 * 3600
    runs[-1]["end"] = (datetime.fromisoformat(runs[-1]["start"])
                       + timedelta(hours=24)).isoformat()
    runs[-2]["outcome"] = "stopped"
    mr = dict(runs[-3])
    mr.update({"dyno": "run.4242", "source": "Manual run",
               "job_key": "detect_abuse", "job_label": "detect_abuse",
               "group_label": "detect_abuse", "args": "",
               "command": "python manage.py detect_abuse"})
    runs.append(mr)

    runs.sort(key=lambda r: r["start"])
    return {"generated_at": now.isoformat(), "demo": True,
            "runs": runs, "slow_queries": slow_queries}


def demo_output(dyno: str) -> list[dict]:
    now = datetime.now(timezone.utc)
    rng = random.Random(dyno)
    lines = ["Starting process", "Connecting to database",
             "Loaded 12840 candidate rows"]
    lines += [f"processed batch {i + 1}/12 ({rng.randint(800, 1200)} rows)"
              for i in range(12)]
    lines += ["Done in 94.2s", "Process exited with status 0"]
    t0 = now - timedelta(seconds=100)
    return [{"t": (t0 + timedelta(seconds=i * 6)).isoformat(), "m": m}
            for i, m in enumerate(lines)]
