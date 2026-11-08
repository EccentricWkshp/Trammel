import signal
import time

from django.core.management.base import BaseCommand

from trammel.core import tasks


class Command(BaseCommand):
    help = "Run background tasks and scheduled jobs."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Drain the queue and exit.")
        parser.add_argument("--interval", type=float, default=5.0, help="Seconds between polls.")

    def handle(self, *args, once=False, interval=5.0, **options):
        stopping = False

        def stop(*_):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        self.stdout.write("Worker started.")
        while not stopping:
            tasks.reclaim_stale()
            tasks.schedule_periodic()
            ran = tasks.run_pending(limit=100)
            if once:
                self.stdout.write(f"Ran {ran} task(s).")
                return
            if not ran:
                time.sleep(interval)
        self.stdout.write("Worker stopped.")
