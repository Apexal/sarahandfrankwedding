"""The rules behind the seating editor at /manage/seating/.

The views in `guests/views/seating.py` only parse requests and render the board; every change to
seating goes through a function here:

- Seating is reception-only and one table per guest (`Guest.table`). Assigning never checks
  capacity: an over-full table is allowed and shows up as a warning, because mid-shuffle states
  are normal.
- Table order is `Table.order`, renumbered from 1 on every reorder. A new table goes last.
- Deleting a table unseats its guests (`on_delete=SET_NULL`); the view confirms first.
"""

import structlog
from django.db import transaction
from django.db.models import Max

from guests.models import Guest, Table

logger = structlog.get_logger(__name__)


def assign(guest_ids: list[int], table: Table | None) -> int:
    """Seat these guests at `table`, or unseat them with `None`. Returns how many rows changed.

    Unknown ids raise `ValueError` so a stale page reloads instead of half-applying.
    """
    guest_ids = list(dict.fromkeys(guest_ids))
    guests = Guest.objects.filter(pk__in=guest_ids)
    if guests.count() != len(guest_ids):
        raise ValueError("Unknown guests")
    changed = guests.exclude(table=table).update(table=table)
    logger.info(
        "seats_assigned",
        table_id=table.pk if table else None,
        guests=len(guest_ids),
        changed=changed,
        source="seating",
    )
    return changed


def next_table_order() -> int:
    return (Table.objects.aggregate(top=Max("order"))["top"] or 0) + 1


@transaction.atomic
def reorder_tables(ordered_ids: list[int]) -> None:
    """Apply a new order; the list must name every table exactly once."""
    ordered_ids = list(dict.fromkeys(ordered_ids))
    tables = {table.pk: table for table in Table.objects.select_for_update()}
    if set(ordered_ids) != set(tables):
        raise ValueError("The order must list exactly the existing tables.")
    changed = []
    for position, pk in enumerate(ordered_ids, start=1):
        if tables[pk].order != position:
            tables[pk].order = position
            changed.append(tables[pk])
    Table.objects.bulk_update(changed, ["order"])


def delete_table(table: Table) -> int:
    """Delete the table; its guests become unseated. Returns how many were seated there."""
    seated = table.guests.count()
    pk, name = table.pk, table.name
    table.delete()
    logger.info("table_deleted", table_id=pk, table=name, unseated=seated, source="seating")
    return seated
