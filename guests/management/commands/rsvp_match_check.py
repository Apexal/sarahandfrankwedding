"""Check the RSVP name matcher against a CSV of typed names and the households they should find.

    manage.py rsvp_match_check names.csv

Columns: `typed_first`, `typed_last`, `expected_household`. Each row prints as `ok`
(the expected household is the only candidate), `ambiguous` (it is offered alongside
others, so the guest has to pick), or `miss` (it is not offered at all). A blank
`expected_household` asserts that nothing should match. Any miss exits non-zero, so the
file can double as a regression check before the invitations go out.
"""

import csv
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from guests.matching import find_households

REQUIRED_COLUMNS = ("typed_first", "typed_last", "expected_household")


class Command(BaseCommand):
    help = "Run the RSVP name matcher over a CSV of typed names and report ok / ambiguous / miss per row."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="CSV with columns typed_first, typed_last, expected_household.")

    def handle(self, *args, csv_path: str, **options):
        path = Path(csv_path)
        if not path.is_file():
            raise CommandError(f"No such file: {path}")
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing = [column for column in REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
            if missing:
                raise CommandError(f"Missing column(s): {', '.join(missing)}")
            rows = list(reader)

        totals: Counter[str] = Counter()
        for row in rows:
            typed_first = (row.get("typed_first") or "").strip()
            typed_last = (row.get("typed_last") or "").strip()
            expected = (row.get("expected_household") or "").strip()
            found = [household.name for household in find_households(typed_first, typed_last)]
            status = self._status(expected, found)
            totals[status] += 1
            style = {"ok": self.style.SUCCESS, "ambiguous": self.style.WARNING, "miss": self.style.ERROR}[status]
            offered = ", ".join(found) or "(no match)"
            self.stdout.write(
                f"{style(status.ljust(9))} {typed_first} {typed_last} -> {offered}   expected: {expected or '(none)'}"
            )

        self.stdout.write(
            f"\n{len(rows)} row(s): {totals['ok']} ok, {totals['ambiguous']} ambiguous, {totals['miss']} miss"
        )
        if totals["miss"]:
            raise CommandError(f"{totals['miss']} row(s) missed.")

    @staticmethod
    def _status(expected: str, found: list[str]) -> str:
        if not expected:
            return "ok" if not found else "miss"
        if expected not in found:
            return "miss"
        return "ok" if len(found) == 1 else "ambiguous"
