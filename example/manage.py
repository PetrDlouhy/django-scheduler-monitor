#!/usr/bin/env python
"""Example project: run the dashboard in DEMO mode without any Axiom setup.

    python manage.py migrate
    python manage.py demo_login   # creates staff user demo/demo
    python manage.py runserver
    -> http://localhost:8000/scheduler/
"""
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # package checkout
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
