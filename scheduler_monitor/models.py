import statistics
from datetime import timedelta

from django.db import models
from django.utils import timezone

RUN_FIELDS = [
    "dyno", "source", "command", "job_key", "job_label", "group_label", "args",
    "duration_s", "exit_status", "outcome", "stopped", "cycled",
    "peak_rss_mb", "quota_mb", "mem_pct", "peak_load", "dyno_size", "cadence",
    "mem_series", "load_series",
]
# stored series are for the detail charts; cap size (UI downsamples to ~300 anyway)
SERIES_CAP = 400


def _downsample(series, cap=SERIES_CAP):
    """Max-preserving downsample of [(offset, value), ...] (keeps spikes)."""
    if not series or len(series) <= cap:
        return series
    out, bucket = [], len(series) / cap
    for i in range(cap):
        chunk = series[int(i * bucket):max(int(i * bucket) + 1, int((i + 1) * bucket))]
        out.append(max(chunk, key=lambda p: p[1]))
    return out


class Run(models.Model):
    """One one-off dyno run, persisted by the ``sync_scheduler_runs`` command.

    Stores exactly what the timeline/detail views need — run metadata plus the
    (capped) memory/load series. Job output and slow queries are deliberately
    NOT stored; those stay on-demand from the log drain.
    """

    dyno = models.CharField(max_length=100)
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=50)
    command = models.TextField()
    job_key = models.CharField(max_length=255)
    job_label = models.CharField(max_length=500)
    group_label = models.CharField(max_length=500)
    args = models.TextField(blank=True)
    duration_s = models.FloatField(null=True, blank=True)
    exit_status = models.IntegerField(null=True, blank=True)
    outcome = models.CharField(max_length=20)
    stopped = models.BooleanField(default=False)
    cycled = models.BooleanField(default=False)
    peak_rss_mb = models.FloatField(null=True, blank=True)
    quota_mb = models.IntegerField(null=True, blank=True)
    mem_pct = models.FloatField(null=True, blank=True)
    peak_load = models.FloatField(null=True, blank=True)
    dyno_size = models.CharField(max_length=30, null=True, blank=True)
    cadence = models.CharField(max_length=20, blank=True)
    mem_series = models.JSONField(null=True, blank=True)
    load_series = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["dyno", "start"],
                                               name="uniq_run_dyno_start")]

    def __str__(self):
        return f"{self.dyno} {self.job_label} @ {self.start:%Y-%m-%d %H:%M}"

    def to_run_dict(self) -> dict:
        d = {f: getattr(self, f) for f in RUN_FIELDS}
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat() if self.end else None
        return d

    @classmethod
    def close_stale(cls, now=None) -> int:
        """Close persisted rows that can no longer be running.

        A sync that catches a run mid-flight stores it as ``running`` with no
        end; if no later sync covers that run again (sync not scheduled, or the
        run aged out of the sync lookback), the row would stay "running"
        forever and the dashboard would draw it stretching to *now*. Heroku
        kills one-off dynos at 24h, so any open row older than that is
        certainly finished: end it at its last stored telemetry sample
        (mirroring ``core.close_open_runs``), falling back to the job's median
        duration. Called by the sync command and before serving persisted
        history."""
        now = now or timezone.now()
        fixed = 0
        stale = cls.objects.filter(end__isnull=True,
                                   start__lt=now - timedelta(hours=25))
        for run in stale:
            offsets = [p[0] for p in (run.mem_series or [])] \
                + [p[0] for p in (run.load_series or [])]
            if offsets:
                dur = max(offsets)
            else:
                siblings = list(cls.objects.filter(
                    job_label=run.job_label, duration_s__isnull=False,
                ).values_list("duration_s", flat=True)[:200])
                dur = statistics.median(siblings) if siblings else 5.0
            run.duration_s = dur
            run.end = run.start + timedelta(seconds=dur)
            run.outcome = "timed_out" if dur >= 23.5 * 3600 else "ended"
            run.save(update_fields=["duration_s", "end", "outcome"])
            fixed += 1
        return fixed

    @classmethod
    def upsert_from_dict(cls, r: dict):
        from .core import parse_ts

        fields = {f: r.get(f) for f in RUN_FIELDS}
        fields["mem_series"] = _downsample(fields["mem_series"])
        fields["load_series"] = _downsample(fields["load_series"])
        fields["end"] = parse_ts(r["end"]) if r.get("end") else None
        fields["args"] = fields["args"] or ""
        fields["cadence"] = fields["cadence"] or ""
        _, created = cls.objects.update_or_create(
            dyno=r["dyno"], start=parse_ts(r["start"]), defaults=fields)
        return created


class LaneOverride(models.Model):
    """Manual lane-grouping override on top of the numeric-normalization heuristic.

    ``merge`` rows map a heuristic group (``group_label``) into a named merged
    lane; ``split`` rows make a heuristic group fall back to exact-args lanes.
    Editable in the Django admin or through the dashboard's ⚙ lanes dialog.
    """

    KIND_MERGE = "merge"
    KIND_SPLIT = "split"
    KIND_CHOICES = [(KIND_MERGE, "merge"), (KIND_SPLIT, "split")]

    kind = models.CharField(max_length=8, choices=KIND_CHOICES)
    group_label = models.CharField(
        max_length=500, unique=True,
        help_text="The heuristic lane (job name + numeric-normalized args) this override applies to.",
    )
    merge_name = models.CharField(
        max_length=255, blank=True,
        help_text="Merged lane name (merge overrides only).",
    )

    class Meta:
        verbose_name = "lane override"

    def __str__(self):
        if self.kind == self.KIND_MERGE:
            return f"{self.group_label} → {self.merge_name}"
        return f"split {self.group_label}"


def overrides_as_dict() -> dict:
    """The dashboard/API wire format: {merges: [{name, members}], splits: [...]}."""
    merges: dict[str, list] = {}
    splits = []
    for o in LaneOverride.objects.all():
        if o.kind == LaneOverride.KIND_MERGE:
            merges.setdefault(o.merge_name, []).append(o.group_label)
        else:
            splits.append(o.group_label)
    return {"merges": [{"name": n, "members": m} for n, m in merges.items()],
            "splits": splits}


def save_overrides(data: dict):
    """Replace all overrides with the posted state (the dialog edits the whole set)."""
    LaneOverride.objects.all().delete()
    rows = [LaneOverride(kind=LaneOverride.KIND_SPLIT, group_label=g)
            for g in data.get("splits", [])]
    for group in data.get("merges", []):
        rows += [LaneOverride(kind=LaneOverride.KIND_MERGE, group_label=m,
                              merge_name=group["name"])
                 for m in group.get("members", [])]
    LaneOverride.objects.bulk_create(rows)
