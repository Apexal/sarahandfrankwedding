"""django-import-export resources.

The guest import is one row per guest with the household columns repeated. The first
row seen for a household wins its contact and address fields; later rows only add
guests. Re-importing matches guests by (household, first name, last name), so a
corrected spreadsheet updates rather than duplicates.
"""

from import_export import fields, resources
from import_export.widgets import BooleanWidget, ForeignKeyWidget

from guests.models import Event, Guest, Household

HOUSEHOLD_COLUMNS = (
    "household",
    "side",
    "household_email",
    "household_phone",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "country",
    "events",
    "notes",
)
GUEST_COLUMNS = ("first_name", "last_name", "is_plus_one", "is_child", "also_known_as", "guest_email")


class YesNoWidget(BooleanWidget):
    """Accepts yes/y/x/true/1 as true; blank or anything else is false, never null."""

    TRUE_VALUES = [*BooleanWidget.TRUE_VALUES, "yes", "y", "x", "Yes", "Y", "X", "YES"]

    def clean(self, value, row=None, **kwargs):
        if value in self.NULL_VALUES:
            return False
        return value in self.TRUE_VALUES


def _household_column(name: str):
    """A read-only export column pulled from the guest's household."""

    def dehydrate(self, guest: Guest):
        household = guest.household
        match name:
            case "household":
                return household.name
            case "household_email":
                return household.email
            case "household_phone":
                return household.phone
            case "events":
                return ";".join(event.slug for event in household.invited_events.all())
            case _:
                return getattr(household, name)

    return dehydrate


class GuestResource(resources.ModelResource):
    household = fields.Field(
        attribute="household",
        column_name="household",
        widget=ForeignKeyWidget(Household, field="name"),
    )
    is_plus_one = fields.Field(attribute="is_plus_one", column_name="is_plus_one", widget=YesNoWidget())
    is_child = fields.Field(attribute="is_child", column_name="is_child", widget=YesNoWidget())
    guest_email = fields.Field(attribute="email", column_name="guest_email")
    side = fields.Field(column_name="side", readonly=True)
    household_email = fields.Field(column_name="household_email", readonly=True)
    household_phone = fields.Field(column_name="household_phone", readonly=True)
    address_line1 = fields.Field(column_name="address_line1", readonly=True)
    address_line2 = fields.Field(column_name="address_line2", readonly=True)
    city = fields.Field(column_name="city", readonly=True)
    state = fields.Field(column_name="state", readonly=True)
    postal_code = fields.Field(column_name="postal_code", readonly=True)
    country = fields.Field(column_name="country", readonly=True)
    events = fields.Field(column_name="events", readonly=True)
    notes = fields.Field(column_name="notes", readonly=True)

    dehydrate_side = _household_column("side")
    dehydrate_household_email = _household_column("household_email")
    dehydrate_household_phone = _household_column("household_phone")
    dehydrate_address_line1 = _household_column("address_line1")
    dehydrate_address_line2 = _household_column("address_line2")
    dehydrate_city = _household_column("city")
    dehydrate_state = _household_column("state")
    dehydrate_postal_code = _household_column("postal_code")
    dehydrate_country = _household_column("country")
    dehydrate_events = _household_column("events")
    dehydrate_notes = _household_column("notes")

    class Meta:
        model = Guest
        fields = HOUSEHOLD_COLUMNS + GUEST_COLUMNS
        export_order = HOUSEHOLD_COLUMNS + GUEST_COLUMNS
        import_id_fields = ("household", "first_name", "last_name")
        skip_unchanged = True
        use_transactions = True
        clean_model_instances = True

    def before_import_row(self, row, **kwargs):
        name = (row.get("household") or "").strip()
        if not name:
            raise ValueError("Every row needs a household name.")
        row["household"] = name
        for column in ("first_name", "last_name"):
            row[column] = (row.get(column) or "").strip()

        side = (row.get("side") or Household.Side.BOTH).strip().lower()
        if side not in Household.Side.values:
            raise ValueError(f"Unknown side {side!r}; use one of {', '.join(Household.Side.values)}.")

        event_slugs = [slug.strip() for slug in (row.get("events") or "").split(";") if slug.strip()]
        events = list(Event.objects.filter(slug__in=event_slugs)) if event_slugs else []
        missing = set(event_slugs) - {event.slug for event in events}
        if missing:
            raise ValueError(f"Unknown event slug(s): {', '.join(sorted(missing))}.")

        household, created = Household.objects.get_or_create(
            name=name,
            defaults={
                "side": side,
                "email": (row.get("household_email") or "").strip(),
                "phone": (row.get("household_phone") or "").strip(),
                "address_line1": (row.get("address_line1") or "").strip(),
                "address_line2": (row.get("address_line2") or "").strip(),
                "city": (row.get("city") or "").strip(),
                "state": (row.get("state") or "").strip(),
                "postal_code": (row.get("postal_code") or "").strip(),
                "country": (row.get("country") or "US").strip() or "US",
                "notes": (row.get("notes") or "").strip(),
            },
        )
        if created and events:
            household.invited_events.set(events)

    def get_queryset(self):
        return super().get_queryset().select_related("household").prefetch_related("household__invited_events")


class HouseholdResource(resources.ModelResource):
    """Export only: one row per household, for address labels and the printer."""

    guest_names = fields.Field(column_name="guests", readonly=True)
    events = fields.Field(column_name="events", readonly=True)
    address_confirmed = fields.Field(column_name="address_confirmed", readonly=True)

    class Meta:
        model = Household
        fields = (
            "name",
            "invite_code",
            "side",
            "email",
            "phone",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "country",
            "address_confirmed",
            "guest_names",
            "events",
            "responded_at",
            "notes",
        )
        export_order = fields

    def dehydrate_guest_names(self, household: Household) -> str:
        return "; ".join(guest.display_name for guest in household.guests.all())

    def dehydrate_events(self, household: Household) -> str:
        return ";".join(event.slug for event in household.invited_events.all())

    def dehydrate_address_confirmed(self, household: Household) -> str:
        return "yes" if household.address_confirmed else "no"

    def get_queryset(self):
        return super().get_queryset().prefetch_related("guests", "invited_events")
