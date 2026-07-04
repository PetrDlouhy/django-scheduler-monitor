"""Example-project settings: the packaged dashboard in DEMO mode."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

SECRET_KEY = "example-only-not-secret"
DEBUG = True
USE_TZ = True
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "scheduler_monitor",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3",
                         "NAME": BASE_DIR / "db.sqlite3"}}

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

STATIC_URL = "/static/"

# Demo data by default; set the AXIOM_DATASET env var (+ AXIOM_TOKEN) to run
# against a real log drain. Site-specific extras (label rules, lookbacks)
# belong in an untracked example/local_settings.py.
SCHEDULER_MONITOR = {
    "DEMO": not os.environ.get("AXIOM_DATASET"),
    "AXIOM_DATASET": os.environ.get("AXIOM_DATASET"),
}

try:
    from local_settings import *  # noqa: F401,F403
except ImportError:
    pass
