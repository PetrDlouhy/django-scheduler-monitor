"""Reconstruct Heroku one-off dyno runs from a log drain stored in Axiom.

One record per run is rebuilt from the standard Heroku log lines:

    Starting process with command `...`    -> command / job identity
    State changed from starting to up      -> start
    Process exited with status N           -> exit code
    State changed from up to complete|crashed
    Stopping all processes with SIGTERM    -> platform stop (ps:stop / deploy)
    Cycling                                -> Heroku 24h auto-cycle
    sample#memory_rss / load_avg_1m        -> memory & load time-series

plus the Postgres slow-query log (reassembled from Heroku's ~1KB line splits)
attributed to runs via the ``application_name`` = dyno-id convention.
"""

import json
import re
import statistics
import urllib.request
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .conf import get_conf

# --- Axiom query layer --------------------------------------------------------


def apl(query: str, start: str, end: str = "now") -> list[dict]:
    """Run an APL query over [start, end], return list-of-dict rows."""
    conf = get_conf()
    body = json.dumps({"apl": query, "startTime": start, "endTime": end}).encode()
    req = urllib.request.Request(
        conf["AXIOM_URL"],
        data=body,
        headers={
            "Authorization": f"Bearer {conf['AXIOM_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.load(resp)
    table = payload["tables"][0]
    names = [f["name"] for f in table["fields"]]
    cols = table.get("columns") or []
    if not cols:
        return []
    return [dict(zip(names, row)) for row in zip(*cols)]


def _oneoff_filter() -> str:
    prefixes = get_conf()["ONEOFF_PREFIXES"]
    return "(" + " or ".join(f'procid startswith "{p}"' for p in prefixes) + ")"


def _lifecycle_apl() -> str:
    # `api` control-plane announcements are needed because API/manual `run.`
    # dynos never log their own "Starting process" line; the command is
    # back-filled from these in build_runs(). web/worker/release are excluded
    # (long-lived process types).
    return f"""
{get_conf()["AXIOM_DATASET"]}
| where ({_oneoff_filter()}
         and message has_any ("Starting process with command", "State changed from",
                              "exited with status", "Stopping all processes", "Cycling"))
    or (procid == "api" and message has "Starting process with command")
| project _time, procid, message
| sort by _time desc
| limit 65000
"""


def _raw_samples_apl() -> str:
    # DESC + limit keeps the *most recent* samples: the fresh telemetry tail is
    # the liveness signal for long-running jobs. Ascending order would drop it
    # once the window exceeds the row cap and running jobs would look ended.
    return f"""
{get_conf()["AXIOM_DATASET"]}
| where {_oneoff_filter()}
        and (message contains "sample#memory_rss" or message contains "sample#load_avg_1m")
| extend rss=todouble(extract("memory_rss=([0-9.]+)MB", 1, message)),
         quota=todouble(extract("memory_quota=([0-9.]+)MB", 1, message)),
         load1=todouble(extract("load_avg_1m=([0-9.]+)", 1, message))
| project _time, procid, rss, quota, load1
| sort by _time desc
| limit 65000
"""


def _slowq_apl() -> str:
    # Heroku truncates each Postgres log line at ~1KB and splits long statements
    # into numbered continuation parts ("[COLOR] [N-1]", "[N-2]", ...). Group the
    # parts of one message by (connection color, log-line number, minute) and
    # stitch them back together in reassemble_slow_queries() — without this only
    # the first ~900 chars survive (the column list, no WHERE/JOIN).
    return f"""
{get_conf()["AXIOM_DATASET"]}
| where procid startswith "postgres"
| extend part=toint(extract("\\\\[[0-9]+-([0-9]+)\\\\]", 1, message))
| where isnotnull(part) and (part >= 2 or message contains " statement:")
| extend color=extract("^\\\\[([A-Z]+)\\\\]", 1, message),
         lineno=extract("\\\\[[A-Z]+\\\\] \\\\[([0-9]+)-", 1, message),
         tbin=bin(_time, 1m),
         enc=strcat(tostring(part), "|SEP|", message)
| summarize raw=make_list(enc),
            dur=max(todouble(extract("duration: ([0-9.]+) ms", 1, message))),
            mint=min(_time) by color, lineno, tbin
| where dur > 1000
| project mint, dur, raw
| sort by dur desc
| limit 2500
"""


# --- parsing ------------------------------------------------------------------

CMD_RE = re.compile(r"Starting process with command `(.+)`", re.S)
EXIT_RE = re.compile(r"exited with status (\d+)")
NUM_RE = re.compile(r"\d+")
PART_PREFIX_RE = re.compile(r"^\[[A-Z]+\] \[\d+-\d+\]\s*")
APPNAME_RE = re.compile(r'application_name="([^"]*)"')

# Heroku dyno RAM tiers (MB -> name). 512 is shared by Eco/Basic/Standard-1X.
SIZE_BY_QUOTA = {
    512: "Standard-1X",
    1024: "Standard-2X",
    2560: "Performance-M",
    14336: "Performance-L",
    30720: "Performance-L-RAM",
    63488: "Performance-XL",
}

CADENCE_SECONDS = {
    "5 min": 300, "10 min": 600, "20 min": 1200, "30 min": 1800,
    "hourly": 3600, "few/day": 6 * 3600, "daily": 86400,
    "irregular": 86400, "rare/ad-hoc": 86400,
}
HEROKU_MAX_S = 24 * 3600  # one-off dynos are force-killed at 24h
TELEMETRY_STALE_S = 600  # no memory/load sample for >10min => dyno is gone
# Tolerance for a sample/marker arriving just after a run's logged terminal state.
_SLACK = timedelta(seconds=30)


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def job_key(command: str, label_rules=None) -> tuple[str, str]:
    """(job_key, args) -- a stable human name + the variable tail."""
    cmd = command.strip()
    if cmd.startswith("("):  # placeholder for a run whose command wasn't logged
        return cmd, ""
    if " && " in cmd:
        # Chained commands: name every segment so the label shows what the run
        # actually does. Rules apply per segment — a rule matching one segment
        # must not swallow the whole chain. Repeated segment names collapse
        # (e.g. two cache-warm curls); args are dropped for a stable lane name.
        names = [job_key(seg, label_rules)[0] for seg in cmd.split(" && ")]
        return " && ".join(dict.fromkeys(n for n in names if n)), ""
    for pattern, label in label_rules or []:
        if re.search(pattern, cmd):
            return label, ""
    if "manage.py" in cmd:
        tail = cmd.split("manage.py", 1)[1].strip()
        parts = tail.split()
        name = parts[0] if parts else "manage.py"
        return name, " ".join(parts[1:])
    return (cmd.split()[0] if cmd.split() else cmd), ""


def classify(exit_status, crashed: bool, ended: bool) -> str:
    if exit_status == 0:
        return "success"
    if exit_status == 137:
        return "oom_killed"
    if exit_status == 143:
        return "timed_out"
    if exit_status is not None:
        return "failed"
    if crashed:
        return "crashed"
    # "State changed from up to complete" without a captured exit-status line:
    # Heroku's "complete" state means clean termination (a crash says "crashed"),
    # so this is a success whose exit code the log drain just didn't capture.
    if ended:
        return "success"
    return "running"


def source_of(procid: str) -> str:
    if procid.startswith("advanced-scheduler."):
        return "Advanced Scheduler"
    if procid.startswith("run."):
        return "Manual run"
    if procid.startswith("scheduler."):
        return "Scheduler"
    return procid.split(".")[0]


def build_runs(lifecycle: list[dict], label_rules=None) -> list[dict]:
    """Segment lifecycle lines into one record PER RUN.

    A run = a `Starting process` line (or a bare `starting to up` for API/manual
    dynos) up to its terminal state. Dyno names recycle, so each procid's lines
    are walked in time order and every `Starting` opens a fresh run.
    """
    by_proc: dict[str, list] = defaultdict(list)
    api_starts = []  # (time, command) — control plane announces `run.` dyno commands
    for row in lifecycle:
        if row["procid"] == "api":
            m = CMD_RE.search(row["message"])
            if m:
                api_starts.append((parse_ts(row["_time"]), m.group(1).strip()))
            continue
        by_proc[row["procid"]].append((parse_ts(row["_time"]), row["message"]))
    api_starts.sort()

    segments = []
    markers = []  # (proc, time, "stop"|"cycle") — attached by time below, because
    # Heroku logs "up to complete" BEFORE "Stopping ... SIGTERM" for a ps:stop,
    # so the marker arrives after the segment has already closed.
    for proc, lines in by_proc.items():
        lines.sort(key=lambda x: x[0])
        cur = None
        for t, msg in lines:
            if "Stopping" in msg and "SIGTERM" in msg:
                markers.append((proc, t, "stop"))
                continue
            if "Cycling" in msg:
                markers.append((proc, t, "cycle"))
                continue
            m = CMD_RE.search(msg)
            if m:
                if cur:
                    segments.append(cur)
                cur = {"dyno": proc, "command": m.group(1).strip(), "starting": t,
                       "up": None, "exited": None, "exit_status": None, "crashed": False}
            elif "starting to up" in msg:
                # Open a run even without a per-dyno "Starting process" line —
                # API/manual `run.` dynos never log one (command comes from `api`),
                # and a scheduler dyno's line is occasionally dropped.
                if cur is None:
                    cur = {"dyno": proc, "command": None, "starting": t, "up": t,
                           "exited": None, "exit_status": None, "crashed": False}
                else:
                    cur["up"] = t
            elif cur is None:
                continue  # lifecycle line for a run whose Starting predates the window
            elif (e := EXIT_RE.search(msg)):
                cur["exit_status"] = int(e.group(1))
                cur["exited"] = cur["exited"] or t
            # Terminal state-changes. Match "to <state>" not "up to <state>" — a
            # fast job can go "starting to complete", skipping the up state.
            elif "to crashed" in msg or "to down" in msg:
                cur["crashed"] = True
                cur["exited"] = cur["exited"] or t
                segments.append(cur)
                cur = None
            elif "to complete" in msg:
                cur["exited"] = cur["exited"] or t
                segments.append(cur)
                cur = None
        if cur:
            segments.append(cur)

    # Assign the command for runs opened without a per-dyno "Starting" line by
    # greedily pairing dyno starts with `api` announcements in time order: every
    # dyno that logged its own command first consumes its matching announcement,
    # so a manual `run.` dyno picks up what's genuinely left over near its start
    # (rather than a concurrent scheduler job's announcement).
    api_pool = sorted(api_starts)
    pool_t = [t for t, _ in api_pool]
    used = [False] * len(api_pool)

    def take(ts, want=None):
        k = bisect_right(pool_t, ts + _SLACK) - 1
        while k >= 0 and (ts - pool_t[k]).total_seconds() <= 300:
            if not used[k] and (want is None or api_pool[k][1] == want):
                return k
            k -= 1
        return -1

    by_start = sorted(segments, key=lambda s: s["starting"])
    for seg in by_start:  # pass 1: known commands consume their match
        if seg["command"] is not None:
            k = take(seg["starting"], seg["command"])
            if k >= 0:
                used[k] = True
    for seg in by_start:  # pass 2: give the remainder to unknown runs
        if seg["command"] is None:
            k = take(seg["starting"])
            if k >= 0:
                used[k] = True
                seg["command"] = api_pool[k][1]
            else:
                seg["command"] = "(command not captured)"

    runs = []
    for r in segments:
        started = r["up"] or r["starting"]
        ended = r["exited"]
        if not started:
            continue
        jkey, args = job_key(r["command"], label_rules)
        runs.append({
            "dyno": r["dyno"],
            "source": source_of(r["dyno"]),
            "command": r["command"],
            "job_key": jkey,
            # Distinct args => distinct logical job => its own lane / cadence.
            "job_label": f"{jkey} {args}".strip() if args else jkey,
            # Lane-grouping identity: numeric arg values are tuning parameters
            # ("--days 365" == "--days 90" -> "--days N"), word values are mode
            # selectors ("--operations counts" stays distinct). Manual merge/split
            # overrides on top of this live in the LaneOverride model.
            "group_label": (f"{jkey} {NUM_RE.sub('N', args)}".strip() if args else jkey),
            "args": args,
            "start": started.isoformat(),
            "end": ended.isoformat() if ended else None,
            "duration_s": (ended - started).total_seconds() if ended else None,
            "exit_status": r["exit_status"],
            "outcome": classify(r["exit_status"], r["crashed"], bool(ended)),
            "stopped": False, "cycled": False,
            # memory fields filled in by attach_samples()
            "peak_rss_mb": None, "quota_mb": None, "mem_pct": None,
            "peak_load": None, "dyno_size": None, "mem_series": None, "load_series": None,
        })
    runs.sort(key=lambda x: x["start"])

    # Attach stop/cycle markers to the run of the same dyno whose window contains
    # them (+slack — the SIGTERM line lands a second or two after "complete").
    runs_by_proc: dict[str, list] = defaultdict(list)
    for r in runs:
        runs_by_proc[r["dyno"]].append(r)
    for proc, mt, kind in markers:
        for r in runs_by_proc.get(proc, []):
            s = parse_ts(r["start"])
            e = parse_ts(r["end"]) + _SLACK if r["end"] else mt + _SLACK
            if s - _SLACK <= mt <= e:
                r["stopped" if kind == "stop" else "cycled"] = True
                break
    return runs


def infer_cadence(runs: list[dict]) -> dict[str, str]:
    """Median inter-arrival gap per group_label -> cadence bucket snapped to
    common schedules. Keyed on the normalized group so parameter variants
    ("--days 365" vs "--days 90") pool their starts and reveal the true cadence."""
    starts: dict[str, list[datetime]] = {}
    for r in runs:
        starts.setdefault(r["group_label"], []).append(parse_ts(r["start"]))
    out = {}
    for key, times in starts.items():
        times.sort()
        if len(times) < 3:
            out[key] = "rare/ad-hoc"
            continue
        gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
        med = statistics.median(gaps)
        if med < 240:
            out[key] = "5 min"
        elif med < 750:
            out[key] = "10 min"
        elif med < 1350:
            out[key] = "20 min"
        elif med < 2700:
            out[key] = "30 min"
        elif med < 5400:
            out[key] = "hourly"
        elif med < 50000:
            out[key] = "few/day"
        elif med < 130000:
            out[key] = "daily"
        else:
            out[key] = "irregular"
    return out


def attach_samples(runs: list[dict], raw_rows: list[dict], now: datetime):
    """Assign each raw sample to the run (same procid) whose interval contains it.

    Derives per-run peak RSS, dyno quota/size, peak load, and the RSS/load
    time-series. Interval match is required because dyno names recycle."""
    runs_by_proc: dict[str, list] = defaultdict(list)
    for r in runs:
        runs_by_proc[r["dyno"]].append(r)
    index = {}
    for proc, rs in runs_by_proc.items():
        rs.sort(key=lambda r: r["start"])
        index[proc] = ([parse_ts(r["start"]) for r in rs], rs)

    for row in raw_rows:
        starts, rs = index.get(row["procid"], (None, None))
        if not rs:
            continue
        t = parse_ts(row["_time"])
        i = bisect_right(starts, t) - 1
        if i < 0:
            continue
        r = rs[i]
        end = parse_ts(r["end"]) if r["end"] else now
        if t > end + _SLACK:  # sample after this run ended -> skip
            continue
        agg = r.setdefault("_agg", {"rss": [], "quota": None, "load": None, "loadser": []})
        offset = round((t - parse_ts(r["start"])).total_seconds(), 1)
        if row.get("rss") is not None:
            agg["rss"].append((offset, round(row["rss"], 1)))
        if row.get("quota") is not None:
            agg["quota"] = max(agg["quota"] or 0, row["quota"])
        if row.get("load1") is not None:
            agg["load"] = max(agg["load"] or 0, row["load1"])
            agg["loadser"].append((offset, round(row["load1"], 2)))

    for r in runs:
        agg = r.pop("_agg", None)
        if not agg:
            continue
        quota = agg["quota"]
        peak = max((rss for _, rss in agg["rss"]), default=None)
        r["peak_rss_mb"] = peak
        r["quota_mb"] = int(quota) if quota else None
        r["mem_pct"] = round(100 * peak / quota, 1) if (peak and quota) else None
        r["peak_load"] = agg["load"]
        r["dyno_size"] = SIZE_BY_QUOTA.get(int(quota)) if quota else None
        # samples arrive newest-first (query sorts desc); sort ascending for charts
        r["mem_series"] = sorted(agg["rss"]) or None
        r["load_series"] = sorted(agg["loadser"]) or None


def close_open_runs(runs: list[dict], now: datetime):
    """Give an end to runs with no terminal line, instead of leaving them
    'running' forever. Terminal lines get dropped from the drain, and a run
    can't outlive Heroku's 24h one-off limit.

    Priority for the inferred end: last telemetry sample (the dyno stopped
    emitting metrics ≈ it stopped); else, for sample-less short jobs, the job's
    median duration once it's clearly too old for its cadence to still be running.
    """
    durs: dict[str, list] = {}
    for r in runs:
        if r["duration_s"] is not None:
            durs.setdefault(r["job_label"], []).append(r["duration_s"])
    median_dur = {k: statistics.median(v) for k, v in durs.items()}

    for r in runs:
        if r["end"] is not None:
            continue
        start = parse_ts(r["start"])
        age = (now - start).total_seconds()
        offsets = [p[0] for p in (r.get("mem_series") or [])] \
            + [p[0] for p in (r.get("load_series") or [])]
        if offsets:
            last = max(offsets)
            if (age - last) <= TELEMETRY_STALE_S and age < HEROKU_MAX_S:
                continue  # telemetry still fresh => genuinely running
            dur = last
            r["outcome"] = "timed_out" if dur >= 23.5 * 3600 else "ended"
        else:
            bound = max(900, 1.5 * CADENCE_SECONDS.get(r["cadence"], HEROKU_MAX_S))
            if age <= bound and age < HEROKU_MAX_S:
                continue  # sample-less but plausibly still running
            dur = median_dur.get(r["job_label"], 5.0)  # dropped terminal on a short job
            r["outcome"] = "ended"
        end = start + timedelta(seconds=min(dur, age))
        r["end"] = end.isoformat()
        r["duration_s"] = (end - start).total_seconds()


def mark_24h_kills(runs: list[dict]):
    """A one-off dyno can't outlive Heroku's 24h limit — any run that long was
    force-killed (SIGTERM), i.e. interrupted, not a clean success. Reclassify
    unless it actually exited 0 (a job that genuinely finished just under the
    wall). Catches runs whose "exited with status 143" line was dropped."""
    for r in runs:
        if (r["duration_s"] or 0) >= 23.5 * 3600 and r["exit_status"] != 0:
            r["outcome"] = "timed_out"


def mark_stopped(runs: list[dict]):
    """A dyno told to stop ("Stopping all processes with SIGTERM") did not finish
    on its own, even if it then exited 0 (`poetry run` does, once its child is
    gone). Two flavours, both distinct from success:

    * `Cycling` + SIGTERM is a platform deadline. A one-off dyno is never
      auto-cycled the way web/worker dynos are; the line appears when Advanced
      Scheduler stops the dyno at its trigger timeout (and at Heroku's 24h
      one-off limit). That is `timed_out` — whatever exit status follows, and
      even when the exit line is dropped or lands after "complete".
    * a bare SIGTERM is a stop by hand or by a deploy restart — `stopped`.
    """
    for r in runs:
        if r.get("cycled"):
            r["outcome"] = "timed_out"
        elif r.get("stopped"):
            r["outcome"] = "stopped"


# --- slow queries ---------------------------------------------------------------


def reassemble_slow_queries(rows: list[dict]) -> list[dict]:
    """Stitch split Postgres log lines back into full statements; keep only
    queries attributable to a one-off dyno via application_name = dyno id
    (the host app must set it — see README). No time-overlap guessing."""
    prefixes = tuple(get_conf()["ONEOFF_PREFIXES"])
    out = []
    for r in rows:
        parts = []
        for enc in r["raw"]:
            pnum, msg = enc.split("|SEP|", 1)
            parts.append((int(pnum), msg))
        parts.sort(key=lambda x: x[0])
        full = "".join(PART_PREFIX_RE.sub("", m) for _, m in parts)
        app_m = APPNAME_RE.search(full)
        app = app_m.group(1) if app_m else ""
        if not app.startswith(prefixes):
            continue  # can't attribute to a one-off run
        sql = re.split(r" statement: ", full, maxsplit=1)
        sql = (sql[1] if len(sql) > 1 else full).strip()
        # Axiom returns aggregated _time (min) as epoch nanoseconds; normalise to ISO.
        mint = r["mint"]
        t_iso = (datetime.fromtimestamp(mint / 1e9, tz=timezone.utc).isoformat()
                 if isinstance(mint, (int, float)) else mint)
        out.append({"t": t_iso, "dur_ms": round(r["dur"], 1), "app": app, "sql": sql[:12000]})
    return out


# --- entry points ---------------------------------------------------------------


def build_dataset(start: str, end: str, series_start: str, series_end: str) -> dict:
    """Fetch + process one time range. Ranges are Axiom time exprs (RFC3339 or
    relative like 'now-3d'). Lifecycle uses [start,end]; the heavier memory/slow
    queries use [series_start,series_end] (kept shorter to stay under row caps)."""
    conf = get_conf()
    lifecycle = apl(_lifecycle_apl(), start, end)
    raw_samples = apl(_raw_samples_apl(), series_start, series_end)
    slow_rows = apl(_slowq_apl(), series_start, series_end)
    slow_q = reassemble_slow_queries(slow_rows)
    runs = build_runs(lifecycle, conf["JOB_LABEL_RULES"])
    cadence = infer_cadence(runs)
    for r in runs:
        r["cadence"] = cadence.get(r["group_label"], "?")
    now = datetime.now(timezone.utc)
    attach_samples(runs, raw_samples, now)
    close_open_runs(runs, now)
    mark_24h_kills(runs)
    mark_stopped(runs)
    return {"generated_at": now.isoformat(), "runs": runs, "slow_queries": slow_q}


def fetch_output(dyno: str, start: str, end: str, limit: int = 5000):
    """Full stdout/stderr for one dyno run, on demand. Excludes Heroku's sample#
    metric lines. Time-bounded to [start,end] so a recycled dyno name only
    returns this run. Returns (lines_chronological, truncated)."""
    dataset = get_conf()["AXIOM_DATASET"]
    query = (f"{dataset}\n"
             f'| where procid == "{dyno}" and not(message contains "sample#")\n'
             f"| project _time, message\n"
             f"| sort by _time desc\n"  # keep the most recent lines if capped
             f"| limit {limit + 1}")
    rows = apl(query, start, end)
    truncated = len(rows) > limit
    rows = rows[:limit]
    rows.sort(key=lambda r: r["_time"])

    def iso(t):  # Axiom may return _time as epoch-ns (number) or ISO (string)
        return (datetime.fromtimestamp(t / 1e9, tz=timezone.utc).isoformat()
                if isinstance(t, (int, float)) else t)

    return [{"t": iso(r["_time"]), "m": str(r["message"]).rstrip("\n")} for r in rows], truncated
