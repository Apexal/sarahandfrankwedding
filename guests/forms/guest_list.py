"""Forms for the guest list editor at /manage/guests/."""

from django import forms
from django.utils import timezone

from guests.models import Event, Guest, Household

_WIDGET_CLASSES = {
    "checkbox": "checkbox checkbox-sm",
    "select": "select select-sm w-full",
    "textarea": "textarea textarea-sm w-full",
}


class DaisyFormMixin:
    """Give every widget its DaisyUI class (small size, full width) without per-field widget declarations."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for bound in self:
            widget = bound.field.widget
            if isinstance(widget, forms.CheckboxSelectMultiple):
                widget.attrs.setdefault("class", "checkbox checkbox-xs")
                continue
            widget.attrs.setdefault("class", _WIDGET_CLASSES.get(bound.widget_type, "input input-sm w-full"))


class QuickAddForm(DaisyFormMixin, forms.Form):
    text = forms.CharField(
        max_length=200,
        label="Names",
        widget=forms.TextInput(attrs={"placeholder": "Bob & Carol Jones, or Sam Lee & Guest", "autocomplete": "off"}),
    )
    side = forms.ChoiceField(choices=Household.Side.choices, initial=Household.Side.BOTH)
    events = forms.ModelMultipleChoiceField(
        queryset=Event.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Invited to",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("events", list(Event.objects.filter(is_private=False).values_list("pk", flat=True)))

    def clean_text(self):
        text = " ".join(self.cleaned_data["text"].split())
        if Household.objects.filter(name__iexact=text).exists():
            raise forms.ValidationError("A household with that name already exists.")
        return text


class HouseholdForm(DaisyFormMixin, forms.ModelForm):
    address_confirmed = forms.BooleanField(
        required=False, label="Address confirmed", help_text="Tick after confirming it with them by phone."
    )
    clear_bounce = forms.BooleanField(
        required=False, label="Clear the bounce", help_text="Campaigns will email them again."
    )

    class Meta:
        model = Household
        fields = (
            "name",
            "side",
            "email",
            "phone",
            "email_opt_out",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "country",
            "notes",
        )
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["address_confirmed"].initial = self.instance.address_confirmed
        if not self.instance.email_bounced_at:
            del self.fields["clear_bounce"]

    def save(self, commit=True):
        household = super().save(commit=False)
        confirmed = self.cleaned_data["address_confirmed"]
        if confirmed and not household.address_confirmed_at:
            household.address_confirmed_at = timezone.now()
        elif not confirmed:
            household.address_confirmed_at = None
        if self.cleaned_data.get("clear_bounce"):
            household.email_bounced_at = None
        if commit:
            household.save()
        return household


class GuestForm(DaisyFormMixin, forms.ModelForm):
    class Meta:
        model = Guest
        fields = ("first_name", "last_name", "is_child", "is_plus_one", "also_known_as", "email")
        labels = {"is_child": "Child", "is_plus_one": "Plus-one", "also_known_as": "Also known as"}


class AddGuestForm(DaisyFormMixin, forms.ModelForm):
    class Meta:
        model = Guest
        fields = ("first_name", "last_name", "is_child")
        labels = {"is_child": "Child"}
