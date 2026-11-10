from django.core.management.base import BaseCommand, CommandError

from trammel.stock.services import check_ledger


class Command(BaseCommand):
    help = "Check that every stock lot's totals equal its ledger, and usages their draws."

    def handle(self, *args, **options):
        problems = check_ledger()
        for problem in problems:
            self.stderr.write(problem)
        if problems:
            raise CommandError(f"{len(problems)} problem(s) found in the stock ledger.")
        self.stdout.write("The stock ledger adds up.")
