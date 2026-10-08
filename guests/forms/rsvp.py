"""RSVP forms: the two lookup forms and the per-household response form.

`build_rsvp_form(household)` assembles one `RsvpForm` from the household's guests, the
invited events that still take answers, and the active questions (PLAN.md section 9).
Field names are the contract the template and the tests rely on:

- `attending_<guest_id>_<event_id>`: yes/no radio, required.
- `plus_one_name_<guest_id>`: only for an unnamed plus-one; required if they attend anything.
- `q_<question_id>_<guest_id>` / `q_<question_id>`: per-guest / per-household questions.
- `contact_email`: optional; where the confirmation goes.

Questions with `only_if_attending` are always rendered and settled server-side in
`clean()`: required only while the relevant guest (or anyone in the household) is
attending that event, and ignored — the value dropped, any stored answer deleted —
otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django import forms
from django.db import transaction
from django.forms import BoundField
from django.utils import timezone

from guests.models import INVITE_CODE_LENGTH, Event, Guest, Household, Rsvp, RsvpAnswer, RsvpQuestion
from pages.models import WeddingSettings

YES = "yes"
NO = "no"
ATTENDANCE_CHOICES = ((YES, "Yes"), (NO, "No"))
PLUS_ONE_NAME_REQUIRED = "Please tell us your guest's name so we can make their place card."
CONDITIONAL_REQUIRED = "Please answer this for everyone who is attending."

_INPUT_ATTRS = {"class": "field-input"}
_TEXTAREA_ATTRS = {"class": "field-input", "rows": 4}
_RADIO_ATTRS = {"class": "choice-input"}
_CHECKBOX_ATTRS = {"class": "choice-input"}


def _to_bool(value: str) -> bool:
    return value == YES


def attendance_field_name(guest_id: int, event_id: int) -> str:
    return f"attending_{guest_id}_{event_id}"


def plus_one_field_name(guest_id: int) -> str:
    return f"plus_one_name_{guest_id}"


def question_field_name(question_id: int, guest_id: int | None = None) -> str:
    return f"q_{question_id}_{guest_id}" if guest_id is not None else f"q_{question_id}"


def is_blank(value) -> bool:
    """A missing answer. `False` is a real answer to a yes/no question, so it is not blank."""
    return value is None or value == "" or (isinstance(value, list | tuple) and not value)


class NameLookupForm(forms.Form):
    first_name = forms.CharField(
        max_length=100,
        label="First name",
        widget=forms.TextInput(attrs={**_INPUT_ATTRS, "autocomplete": "given-name"}),
    )
    last_name = forms.CharField(
        max_length=100,
        label="Last name",
        widget=forms.TextInput(attrs={**_INPUT_ATTRS, "autocomplete": "family-name"}),
    )


class CodeLookupForm(forms.Form):
    code = forms.CharField(
        max_length=INVITE_CODE_LENGTH + 4,
        label="Invitation code",
        widget=forms.TextInput(
            attrs={
                **_INPUT_ATTRS,
                "autocomplete": "off",
                "autocapitalize": "characters",
                "autocorrect": "off",
                "spellcheck": "false",
            }
        ),
    )


@dataclass(slots=True)
class QuestionField:
    question: RsvpQuestion
    field: BoundField
    guest: Guest | None


@dataclass(slots=True)
class GuestSection:
    """What the template renders for one guest: attendance radios, then their questions."""

    guest: Guest
    attendance: list[tuple[Event, BoundField]] = field(default_factory=list)
    plus_one_name: BoundField | None = None
    questions: list[QuestionField] = field(default_factory=list)


class RsvpForm(forms.Form):
    """One household's answers. Built by `build_rsvp_form`; do not instantiate directly."""

    def __init__(
        self,
        household: Household,
        *,
        guests: list[Guest],
        events: list[Event],
        invited_event_ids: set[int],
        questions: list[RsvpQuestion],
        data=None,
        **kwargs,
    ):
        super().__init__(data=data, **kwargs)
        self.household = household
        self.guests = guests
        self.events = events
        self.existing_attendance: dict[tuple[int, int], bool] = {
            (rsvp.guest_id, rsvp.event_id): rsvp.attending for rsvp in Rsvp.objects.filter(guest__household=household)
        }
        existing_answers = {
            (answer.question_id, answer.guest_id): answer.value
            for answer in RsvpAnswer.objects.filter(household=household)
        }
        self.sections: list[GuestSection] = []
        self.household_questions: list[QuestionField] = []
        self.ignored_fields: set[str] = set()

        per_guest = [q for q in questions if q.scope == RsvpQuestion.Scope.PER_GUEST]
        per_household = [q for q in questions if q.scope == RsvpQuestion.Scope.PER_HOUSEHOLD]

        for guest in guests:
            section = GuestSection(guest=guest)
            for event in events:
                name = attendance_field_name(guest.pk, event.pk)
                self.fields[name] = forms.TypedChoiceField(
                    label=event.name,
                    choices=ATTENDANCE_CHOICES,
                    coerce=_to_bool,
                    empty_value=None,
                    widget=forms.RadioSelect(attrs=_RADIO_ATTRS),
                )
                answered = self.existing_attendance.get((guest.pk, event.pk))
                if answered is not None:
                    self.initial[name] = YES if answered else NO
                section.attendance.append((event, self[name]))
            if guest.is_plus_one and not guest.full_name:
                name = plus_one_field_name(guest.pk)
                self.fields[name] = forms.CharField(
                    label="Their name",
                    max_length=200,
                    required=False,
                    widget=forms.TextInput(attrs={**_INPUT_ATTRS, "autocomplete": "off"}),
                )
                section.plus_one_name = self[name]
            for question in per_guest:
                if not self._applies(question, invited_event_ids):
                    continue
                name = question_field_name(question.pk, guest.pk)
                self.fields[name] = self._question_field(question)
                self._set_initial(name, question, existing_answers.get((question.pk, guest.pk)))
                section.questions.append(QuestionField(question=question, field=self[name], guest=guest))
            self.sections.append(section)

        for question in per_household:
            if not self._applies(question, invited_event_ids):
                continue
            name = question_field_name(question.pk)
            self.fields[name] = self._question_field(question)
            self._set_initial(name, question, existing_answers.get((question.pk, None)))
            self.household_questions.append(QuestionField(question=question, field=self[name], guest=None))

        self.fields["contact_email"] = forms.EmailField(
            label="Your email",
            help_text="Optional. We'll send a confirmation there, and it's how we'll reach you with updates.",
            required=False,
            widget=forms.EmailInput(attrs={**_INPUT_ATTRS, "autocomplete": "email"}),
        )
        self.initial["contact_email"] = household.email

    @property
    def question_fields(self) -> list[QuestionField]:
        return [qf for section in self.sections for qf in section.questions] + self.household_questions

    @staticmethod
    def _applies(question: RsvpQuestion, invited_event_ids: set[int]) -> bool:
        """A question gated on an event nobody here is invited to can never be relevant."""
        return question.only_if_attending_id is None or question.only_if_attending_id in invited_event_ids

    @staticmethod
    def _question_field(question: RsvpQuestion) -> forms.Field:
        # Conditional questions are validated in clean(); unconditional ones use the field.
        required = question.required and question.only_if_attending_id is None
        common = {"label": question.label, "help_text": question.help_text, "required": required}
        match question.kind:
            case RsvpQuestion.Kind.LONG_TEXT:
                return forms.CharField(**common, widget=forms.Textarea(attrs=_TEXTAREA_ATTRS))
            case RsvpQuestion.Kind.SINGLE_CHOICE:
                choices = [(choice, choice) for choice in question.choices]
                return forms.ChoiceField(**common, choices=choices, widget=forms.RadioSelect(attrs=_RADIO_ATTRS))
            case RsvpQuestion.Kind.MULTI_CHOICE:
                choices = [(choice, choice) for choice in question.choices]
                return forms.MultipleChoiceField(
                    **common, choices=choices, widget=forms.CheckboxSelectMultiple(attrs=_CHECKBOX_ATTRS)
                )
            case RsvpQuestion.Kind.YES_NO:
                return forms.TypedChoiceField(
                    **common,
                    choices=ATTENDANCE_CHOICES,
                    coerce=_to_bool,
                    empty_value=None,
                    widget=forms.RadioSelect(attrs=_RADIO_ATTRS),
                )
        return forms.CharField(**common, max_length=500, widget=forms.TextInput(attrs=_INPUT_ATTRS))

    def _set_initial(self, name: str, question: RsvpQuestion, value) -> None:
        if value is None:
            return
        if question.kind == RsvpQuestion.Kind.YES_NO:
            self.initial[name] = YES if value else NO
        else:
            self.initial[name] = value

    # --- validation ---------------------------------------------------------------------

    def attendance(self, cleaned=None) -> dict[tuple[int, int], bool]:
        """(guest_id, event_id) → attending, submitted answers over stored ones.

        Events past their own deadline have no field on this form; their stored answer
        still decides whether a question gated on them is asked.
        """
        cleaned = self.cleaned_data if cleaned is None else cleaned
        merged = dict(self.existing_attendance)
        for guest in self.guests:
            for event in self.events:
                value = cleaned.get(attendance_field_name(guest.pk, event.pk))
                if value is not None:
                    merged[(guest.pk, event.pk)] = value
        return merged

    @staticmethod
    def _attending_anything(guest: Guest, attendance: dict[tuple[int, int], bool]) -> bool:
        return any(attending for (guest_id, _), attending in attendance.items() if guest_id == guest.pk)

    def clean(self):
        cleaned = super().clean()
        attendance = self.attendance(cleaned)

        for section in self.sections:
            if (
                section.plus_one_name is not None
                and self._attending_anything(section.guest, attendance)
                and is_blank(cleaned.get(section.plus_one_name.name))
            ):
                self.add_error(section.plus_one_name.name, PLUS_ONE_NAME_REQUIRED)

        for question_field in self.question_fields:
            question = question_field.question
            event_id = question.only_if_attending_id
            if event_id is None:
                continue
            name = question_field.field.name
            if question_field.guest is not None:
                relevant = attendance.get((question_field.guest.pk, event_id), False)
            else:
                relevant = any(
                    attending
                    for (_, attended_event_id), attending in attendance.items()
                    if attended_event_id == event_id
                )
            if relevant:
                if question.required and is_blank(cleaned.get(name)):
                    self.add_error(name, CONDITIONAL_REQUIRED)
            else:
                cleaned[name] = None
                self.ignored_fields.add(name)
        return cleaned

    def error_summary(self) -> list[dict]:
        """Every error with a label that says whose field it is, for the list above the form."""
        items: list[dict] = []
        for section in self.sections:
            who = section.guest.display_name
            for event, bound in section.attendance:
                self._collect_errors(items, bound, f"{who} · {event.name}")
            if section.plus_one_name is not None:
                self._collect_errors(items, section.plus_one_name, f"{who} · their name")
            for question_field in section.questions:
                self._collect_errors(items, question_field.field, f"{who} · {question_field.question.label}")
        for question_field in self.household_questions:
            self._collect_errors(items, question_field.field, question_field.question.label)
        self._collect_errors(items, self["contact_email"], "Your email")
        for error in self.non_field_errors():
            items.append({"id": "", "label": "", "errors": [error]})
        return items

    def _collect_errors(self, items: list[dict], bound: BoundField, label: str) -> None:
        errors = self.errors.get(bound.name)
        if errors:
            items.append({"id": bound.id_for_label, "label": label, "errors": list(errors)})

    # --- persistence --------------------------------------------------------------------

    def save(self) -> bool:
        """Write every answer in one transaction. Returns True on the household's first submission."""
        cleaned = self.cleaned_data
        household = self.household
        now = timezone.now()
        with transaction.atomic():
            for section in self.sections:
                guest = section.guest
                for event, bound in section.attendance:
                    Rsvp.objects.update_or_create(
                        guest=guest,
                        event=event,
                        defaults={
                            "attending": cleaned[bound.name],
                            "responded_at": now,
                            "source": Rsvp.Source.GUEST,
                        },
                    )
                if section.plus_one_name is not None and not is_blank(cleaned.get(section.plus_one_name.name)):
                    full_name = cleaned[section.plus_one_name.name].strip()
                    guest.first_name, _, guest.last_name = full_name.rpartition(" ")
                    if not guest.first_name:
                        guest.first_name, guest.last_name = guest.last_name, ""
                    guest.save()
            for question_field in self.question_fields:
                lookup = {
                    "question": question_field.question,
                    "household": household,
                    "guest": question_field.guest,
                }
                value = cleaned.get(question_field.field.name)
                if is_blank(value):
                    RsvpAnswer.objects.filter(**lookup).delete()
                else:
                    RsvpAnswer.objects.update_or_create(**lookup, defaults={"value": value})

            first_time = household.responded_at is None
            if first_time:
                household.responded_at = now
            email = (cleaned.get("contact_email") or "").strip()
            if email and email.casefold() != household.email.casefold():
                # A new address is not the one that bounced.
                household.email = email
                household.email_bounced_at = None
            household.save(update_fields=("responded_at", "email", "email_bounced_at", "updated_at"))
        return first_time


def open_rsvp_events(household: Household, wedding: WeddingSettings) -> list[Event]:
    """The household's invited events that need an answer and still accept one."""
    return [event for event in household.invited_events.all() if event.rsvp_required and wedding.rsvp_is_open(event)]


def build_rsvp_form(household: Household, *, data=None, wedding: WeddingSettings | None = None) -> RsvpForm:
    wedding = wedding or WeddingSettings.load()
    invited_events = list(household.invited_events.all())
    return RsvpForm(
        household,
        guests=list(household.guests.order_by("sort_order", "id")),
        events=[event for event in invited_events if event.rsvp_required and wedding.rsvp_is_open(event)],
        invited_event_ids={event.pk for event in invited_events},
        questions=list(RsvpQuestion.objects.filter(active=True).order_by("order", "id")),
        data=data,
    )
