from django.core.management.base import BaseCommand

from scheduler_monitor import core, demo
from scheduler_monitor.conf import get_conf
from scheduler_monitor.models import Run


class Command(BaseCommand):
    help = (
        "Persist one-off dyno runs from the log drain into the Run table "
        "(idempotent upsert on dyno+start). Schedule this every 10-30 minutes — "
        "e.g. on Heroku Scheduler itself — to build history beyond the drain's "
        "retention. Runs only; job output and slow queries stay on-demand."
    )

    def add_arguments(self, parser):
        conf = get_conf()
        parser.add_argument("--lookback", default=conf["DEFAULT_LOOKBACK"],
                            help="how far back to fetch, e.g. 3d, 12h "
                                 "(default: DEFAULT_LOOKBACK)")
        parser.add_argument("--series", default=conf["SERIES_LOOKBACK"],
                            help="memory/load sample window (default: SERIES_LOOKBACK)")

    def handle(self, *args, **options):
        conf = get_conf()
        if conf["DEMO"]:
            data = demo.demo_dataset(3.0)
        else:
            data = core.build_dataset(f"now-{options['lookback']}", "now",
                                      f"now-{options['series']}", "now")
        created = sum(Run.upsert_from_dict(r) for r in data["runs"])
        self.stdout.write(self.style.SUCCESS(
            f"synced {len(data['runs'])} runs ({created} new, "
            f"{len(data['runs']) - created} updated); table now holds "
            f"{Run.objects.count()}"))
