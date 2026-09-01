from django import forms

from core.models import User


class UserProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "phone_number", "time_zone"]
        widgets = {
            "first_name": forms.TextInput(attrs={"class": "input w-full"}),
            "last_name": forms.TextInput(attrs={"class": "input w-full"}),
            "phone_number": forms.TextInput(attrs={"class": "input w-full"}),
            "time_zone": forms.Select(attrs={"class": "select w-full"}),
        }
        prefix = "user"
