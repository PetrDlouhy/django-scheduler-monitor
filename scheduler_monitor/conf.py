"""Settings contract.

Host projects configure the app through a single ``SCHEDULER_MONITOR`` dict in
Django settings; every key is optional except ``AXIOM_DATASET`` (unless DEMO).

    SCHEDULER_MONITOR = {
        "AXIOM_DATASET": "my_production_logs",
        # "AXIOM_TOKEN": "...",          # default: read from env AXIOM_TOKEN
        # "AXIOM_URL": "https://api.axiom.co/v1/datasets/_apl?format=tabular",
        # "ONEOFF_PREFIXES": ["scheduler.", "advanced-scheduler.", "run."],
        # "JOB_LABEL_RULES": [(r"renew_cache", "curl: cache warm")],
        # "DEFAULT_LOOKBACK": "3d",      # initial history window
        # "SERIES_LOOKBACK": "36h",      # memory/load/slow-SQL window (heavier)
        # "OLDER_CHUNK_DAYS": 3,         # pan-left loads this much more history
        # "CACHE_SECONDS": 60,           # Django-cache TTL for the dataset
        # "DEMO": False,                 # serve generated demo data, no Axiom
    }
"""

import os

from django.conf import settings

DEFAULTS = {
    "AXIOM_TOKEN": None,  # falls back to the AXIOM_TOKEN environment variable
    "AXIOM_URL": "https://api.axiom.co/v1/datasets/_apl?format=tabular",
    "AXIOM_DATASET": None,
    "ONEOFF_PREFIXES": ["scheduler.", "advanced-scheduler.", "run."],
    "JOB_LABEL_RULES": [],
    "DEFAULT_LOOKBACK": "3d",
    "SERIES_LOOKBACK": "36h",
    "OLDER_CHUNK_DAYS": 3,
    "CACHE_SECONDS": 60,
    "DEMO": False,
    # When True, additionally require a verified 2FA session (django-otp's
    # request.user.is_verified()) on top of staff status. Safe to leave False
    # if the host app has no django-otp; the check degrades to staff-only.
    "REQUIRE_VERIFIED": False,
}


def get_conf() -> dict:
    conf = {**DEFAULTS, **getattr(settings, "SCHEDULER_MONITOR", {})}
    if conf["AXIOM_TOKEN"] is None:
        conf["AXIOM_TOKEN"] = os.environ.get("AXIOM_TOKEN")
    return conf
