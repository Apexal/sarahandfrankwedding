"""Forms for the seating editor at /manage/seating/."""

from django import forms

from guests.forms.guest_list import DaisyFormMixin
from guests.models import Table


class TableForm(DaisyFormMixin, forms.ModelForm):
    class Meta:
        model = Table
        fields = ("name", "capacity", "notes")
        labels = {"capacity": "Seats"}
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        clash = Table.objects.filter(name__iexact=name).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("Another table already has that name.")
        return name
