import json
import re
from datetime import datetime, timedelta
from functools import wraps

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.views import redirect_to_login
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.utils.cache import add_never_cache_headers
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from . import core, demo
from .conf import get_conf
from .models import Run, overrides_as_dict, save_overrides

RANGE_RE = re.compile(r"^\d{1,3}[mhd]$")  # relative Axiom ranges we accept from the client
DYNO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def _verified(request) -> bool:
    """True unless REQUIRE_VERIFIED is set and the user has *not* completed 2FA
    this session. Uses django-otp's ``request.user.is_verified()`` when present
    (via getattr, so the check degrades to staff-only without django-otp)."""
    if not get_conf().get("REQUIRE_VERIFIED"):
        return True
    is_verified = getattr(request.user, "is_verified", None)
    return bool(callable(is_verified) and is_verified())


def staff_api_required(view):
    """Like ``staff_member_required`` but for XHR endpoints: return a JSON 403
    instead of a 302 to the HTML admin-login page. A redirect-to-HTML makes the
    dashboard's ``fetch(...).then(r => r.json())`` calls choke on ``<!DOCTYPE``
    (surfacing as a cryptic "failed to load" when a staff session simply
    expired); a 403 lets the client show a clear "log in again" prompt.

    Also stamps ``Cache-Control: no-store`` on every response — these carry
    production log data and must never be cached by a browser or shared CDN."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        user = request.user
        if not (user.is_authenticated and user.is_staff):
            resp = JsonResponse(
                {"ok": False, "error": "auth", "detail": "staff login required"},
                status=403)
        elif not _verified(request):
            resp = JsonResponse(
                {"ok": False, "error": "2fa", "detail": "verified (2FA) session required"},
                status=403)
        else:
            resp = view(request, *args, **kwargs)
        add_never_cache_headers(resp)
        return resp

    return wrapped


@never_cache
@staff_member_required
def dashboard(request):
    if not _verified(request):
        return redirect_to_login(request.get_full_path())
    return render(request, "scheduler_monitor/dashboard.html", {"conf": get_conf()})


@staff_api_required
def api_data(request):
    """The current dataset (runs + slow queries) for the configured lookback.

    Live Axiom fetch, cached in the Django cache for CACHE_SECONDS; ?force=1
    (the Refresh button) bypasses the cache."""
    conf = get_conf()
    lookback = request.GET.get("lookback") or conf["DEFAULT_LOOKBACK"]
    series = request.GET.get("series") or conf["SERIES_LOOKBACK"]
    if not (RANGE_RE.match(lookback) and RANGE_RE.match(series)):
        return JsonResponse({"ok": False, "error": "bad range"}, status=400)
    if conf["DEMO"]:
        days = float(lookback[:-1]) * {"m": 1 / 1440, "h": 1 / 24, "d": 1}[lookback[-1]]
        return JsonResponse({"ok": True, **demo.demo_dataset(days)})
    key = f"scheduler_monitor:data:{lookback}:{series}"
    data = None if request.GET.get("force") else cache.get(key)
    if data is None:
        data = core.build_dataset(f"now-{lookback}", "now", f"now-{series}", "now")
        cache.set(key, data, conf["CACHE_SECONDS"])
    return JsonResponse({"ok": True, **data})


@staff_api_required
def api_older(request):
    """An older history chunk ending at ?before= (ISO). The client merges it
    into its in-memory dataset, so the server stays stateless.

    Served from the Run table when the ``sync_scheduler_runs`` command has
    persisted that period (instant, and works beyond the drain's retention);
    otherwise fetched live from Axiom."""
    conf = get_conf()
    before = request.GET.get("before")
    if not before:
        return JsonResponse({"ok": False, "error": "before required"}, status=400)
    try:
        end = datetime.fromisoformat(before.replace("Z", "+00:00"))
    except ValueError:
        return JsonResponse({"ok": False, "error": "bad before"}, status=400)
    start = end - timedelta(days=conf["OLDER_CHUNK_DAYS"])
    Run.close_stale()  # self-heal rows a one-off sync left frozen as "running"
    persisted = Run.objects.filter(start__gte=start, start__lt=end).order_by("start")
    if persisted.exists():
        return JsonResponse({"ok": True, "source": "db",
                             "runs": [r.to_run_dict() for r in persisted],
                             "slow_queries": []})  # not stored — on-demand only
    if conf["DEMO"]:
        return JsonResponse({"ok": True, "runs": [], "slow_queries": []})
    data = core.build_dataset(start.isoformat(), end.isoformat(),
                              start.isoformat(), end.isoformat())
    return JsonResponse({"ok": True, "source": "axiom", **data})


@staff_api_required
def api_output(request):
    """Full stdout/stderr of one run, fetched on demand."""
    conf = get_conf()
    dyno = request.GET.get("dyno", "")
    start = request.GET.get("start", "")
    end = request.GET.get("end") or "now"
    if not DYNO_RE.match(dyno):  # dyno is interpolated into the APL query
        return JsonResponse({"ok": False, "error": "bad dyno"}, status=400)
    if conf["DEMO"]:
        lines = demo.demo_output(dyno)
        return JsonResponse({"ok": True, "lines": lines, "truncated": False,
                             "count": len(lines)})
    lines, truncated = core.fetch_output(dyno, start, end)
    return JsonResponse({"ok": True, "lines": lines, "truncated": truncated,
                         "count": len(lines)})


@staff_api_required
@require_http_methods(["GET", "POST"])
def api_merges(request):
    """Manual lane merge/split overrides, stored in the LaneOverride model."""
    if request.method == "POST":
        try:
            data = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return JsonResponse({"ok": False, "error": "bad json"}, status=400)
        save_overrides(data)
    return JsonResponse({"ok": True, **overrides_as_dict()})
