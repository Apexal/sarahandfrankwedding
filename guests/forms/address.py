"""The save-the-date address forms (PLAN.md section 10).

Two ModelForms over the same field list: `HouseholdAddressForm` writes onto a `Household`
reached through its personalized link, `GenericAddressForm` creates a `ContactSubmission`
from the forwardable page.
"""

from django import forms
from django.utils import timezone

from core.utils.phone import normalize_phone_number
from guests.models import ContactSubmission, Household

# No country: guests are all domestic, and a household keeps the one it has (default "US").
ADDRESS_FIELDS = ("address_line1", "address_line2", "city", "state", "postal_code")
CONTACT_FIELDS = ("email", "phone")
PHONE_MAX_LENGTH = Household._meta.get_field("phone").max_length

INPUT_CLASS = "field-input"

US_STATES = [
    ("AL", "Alabama"),
    ("AK", "Alaska"),
    ("AZ", "Arizona"),
    ("AR", "Arkansas"),
    ("CA", "California"),
    ("CO", "Colorado"),
    ("CT", "Connecticut"),
    ("DE", "Delaware"),
    ("DC", "District of Columbia"),
    ("FL", "Florida"),
    ("GA", "Georgia"),
    ("HI", "Hawaii"),
    ("ID", "Idaho"),
    ("IL", "Illinois"),
    ("IN", "Indiana"),
    ("IA", "Iowa"),
    ("KS", "Kansas"),
    ("KY", "Kentucky"),
    ("LA", "Louisiana"),
    ("ME", "Maine"),
    ("MD", "Maryland"),
    ("MA", "Massachusetts"),
    ("MI", "Michigan"),
    ("MN", "Minnesota"),
    ("MS", "Mississippi"),
    ("MO", "Missouri"),
    ("MT", "Montana"),
    ("NE", "Nebraska"),
    ("NV", "Nevada"),
    ("NH", "New Hampshire"),
    ("NJ", "New Jersey"),
    ("NM", "New Mexico"),
    ("NY", "New York"),
    ("NC", "North Carolina"),
    ("ND", "North Dakota"),
    ("OH", "Ohio"),
    ("OK", "Oklahoma"),
    ("OR", "Oregon"),
    ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"),
    ("SC", "South Carolina"),
    ("SD", "South Dakota"),
    ("TN", "Tennessee"),
    ("TX", "Texas"),
    ("UT", "Utah"),
    ("VT", "Vermont"),
    ("VA", "Virginia"),
    ("WA", "Washington"),
    ("WV", "West Virginia"),
    ("WI", "Wisconsin"),
    ("WY", "Wyoming"),
    ("PR", "Puerto Rico"),
    ("GU", "Guam"),
    ("VI", "U.S. Virgin Islands"),
    ("AS", "American Samoa"),
    ("MP", "Northern Mariana Islands"),
    ("AA", "Armed Forces Americas"),
    ("AE", "Armed Forces Europe"),
    ("AP", "Armed Forces Pacific"),
]
# Imported addresses may spell the state out; match either form to the code the form stores.
STATE_LOOKUP = {key.casefold(): code for code, name in US_STATES for key in (code, name)}


def _text_input(autocomplete: str, **extra) -> forms.TextInput:
    return forms.TextInput(attrs={"class": INPUT_CLASS, "autocomplete": autocomplete, **extra})


class BaseAddressForm(forms.ModelForm):
    """Shared fields, labels, widgets, and phone cleaning. Subclasses supply the model."""

    HONEYPOT_FIELD: str | None = None

    # Stored as the two-letter code, which is what goes on the envelope.
    state = forms.ChoiceField(
        choices=[("", "Select a state"), *US_STATES],
        label="State",
        widget=forms.Select(attrs={"class": INPUT_CLASS, "autocomplete": "address-level1"}),
    )
    phone = forms.CharField(
        required=False,
        max_length=40,
        label="Phone",
        widget=_text_input("tel", type="tel", inputmode="tel"),
    )

    class Meta:
        fields = [*ADDRESS_FIELDS, *CONTACT_FIELDS]
        labels = {
            "address_line1": "Street address",
            "address_line2": "Apartment, suite, etc.",
            "city": "City",
            "postal_code": "Zip",
            "email": "Email",
        }
        # The model help text is written for the admin; guests get a plainer reason to share it.
        widgets = {
            "address_line1": _text_input("address-line1"),
            "address_line2": _text_input("address-line2"),
            "city": _text_input("address-level2"),
            "postal_code": _text_input("postal-code"),
            "email": forms.EmailInput(attrs={"class": INPUT_CLASS, "autocomplete": "email", "inputmode": "email"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The point of the page is a mailing address, so the lines an envelope needs are
        # required even though the model allows blanks (imports may lack them).
        for name in ("address_line1", "city", "postal_code"):
            self.fields[name].required = True
        # Preselect a stored state given as a code or spelled out; anything else starts unselected.
        self.initial["state"] = STATE_LOOKUP.get((self.initial.get("state") or "").strip().casefold(), "")

    def clean_phone(self) -> str:
        raw = self.cleaned_data.get("phone", "")
        normalized = normalize_phone_number(raw)
        if raw.strip() and not normalized:
            raise forms.ValidationError("Enter a phone number with digits, or leave it blank.")
        if len(normalized) > PHONE_MAX_LENGTH:
            raise forms.ValidationError("That phone number is too long.")
        return normalized

    @property
    def honeypot(self) -> forms.BoundField | None:
        return self[self.HONEYPOT_FIELD] if self.HONEYPOT_FIELD else None


class HouseholdAddressForm(BaseAddressForm):
    """The personalized page: prefilled from, and saved back onto, the household."""

    class Meta(BaseAddressForm.Meta):
        model = Household

    def save(self, commit: bool = True) -> Household:
        household = super().save(commit=False)
        household.address_confirmed_at = timezone.now()
        if commit:
            household.save()
        return household


class GenericAddressForm(BaseAddressForm):
    """The forwardable page: free-text names plus a honeypot, saved as a `ContactSubmission`.

    The honeypot is a real text input hidden with CSS under an innocuous name; people never
    see it, form-filling bots fill it. Check `tripped` before `is_valid()`: a tripped form
    should be answered with the thank-you page and nothing saved.
    """

    HONEYPOT_FIELD = "website"

    names = forms.CharField(
        max_length=300,
        label="Your name(s)",
        help_text='As they should appear on the envelope, e.g. "Bob & Carol Jones".',
        widget=_text_input("name"),
    )
    website = forms.CharField(
        required=False,
        label="Website",
        widget=forms.TextInput(attrs={"autocomplete": "off", "tabindex": "-1"}),
    )

    class Meta(BaseAddressForm.Meta):
        model = ContactSubmission
        fields = ["names", *BaseAddressForm.Meta.fields]

    @property
    def tripped(self) -> bool:
        return bool((self.data.get(self.add_prefix(self.HONEYPOT_FIELD)) or "").strip())

    def save(self, commit: bool = True, *, ip: str | None = None) -> ContactSubmission:
        submission = super().save(commit=False)
        submission.ip = ip
        submission.status = ContactSubmission.Status.NEW
        if commit:
            submission.save()
        return submission
