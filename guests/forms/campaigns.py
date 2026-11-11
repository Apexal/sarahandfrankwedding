"""Forms for the campaign and email-template pages under /manage/."""

from django import forms

from guests.campaigns import CAMPAIGN_CONTEXT_BUILDERS
from guests.forms.guest_list import DaisyFormMixin
from guests.models import Campaign, Event, Household
from messaging.models import EmailTemplateVersion
from messaging.template_definitions import get_template_definition


def campaign_template_choices() -> list[tuple[str, str]]:
    choices = []
    for key in CAMPAIGN_CONTEXT_BUILDERS:
        definition = get_template_definition(key)
        choices.append((key, definition.name if definition else key))
    return choices


class CampaignForm(DaisyFormMixin, forms.ModelForm):
    template_key = forms.ChoiceField(label="Email", choices=())
    households = forms.ModelMultipleChoiceField(
        queryset=Household.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Households",
        help_text="Only for “Selected households”.",
    )

    class Meta:
        model = Campaign
        fields = ("name", "template_key", "audience", "event", "households", "message", "daily_cap")
        labels = {"daily_cap": "Emails per day"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["template_key"].choices = campaign_template_choices()
        self.fields["event"].queryset = Event.objects.order_by("order", "starts_at")
        self.fields["event"].help_text = "Only for “Attending an event”."
        self.fields["households"].queryset = Household.objects.order_by("name")
        self.fields["message"].required = False
        self.fields[
            "message"
        ].help_text = "Only for the announcement: the paragraph(s) between “Hi …” and the sign-off."

    def clean(self):
        cleaned = super().clean()
        audience = cleaned.get("audience")
        if audience == Campaign.Audience.ATTENDING and not cleaned.get("event"):
            self.add_error("event", "Pick the event for an “attending an event” audience.")
        if audience == Campaign.Audience.SELECTED and not cleaned.get("households"):
            self.add_error("households", "Tick at least one household.")
        return cleaned


class DailyCapForm(DaisyFormMixin, forms.ModelForm):
    class Meta:
        model = Campaign
        fields = ("daily_cap",)
        labels = {"daily_cap": "Emails per day"}


class EmailTemplateForm(DaisyFormMixin, forms.ModelForm):
    class Meta:
        model = EmailTemplateVersion
        fields = ("subject_template", "html_body_template")
        labels = {"subject_template": "Subject", "html_body_template": "HTML body"}
        widgets = {
            "html_body_template": forms.Textarea(
                attrs={"rows": 22, "spellcheck": "false", "class": "textarea textarea-sm w-full font-mono text-xs"}
            )
        }
