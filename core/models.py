from django.contrib.auth.models import AbstractUser
from django.db import models

from core.utils.phone import normalize_phone_number


class TimestampedModel(models.Model):
    """
    All models that need created_at and updated_at fields can inherit from this.
    Generally all models should use it.
    """

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class User(TimestampedModel, AbstractUser):
    email = models.EmailField(blank=False, unique=True)
    phone_number = models.CharField(
        max_length=16,
        blank=True,
    )

    REQUIRED_FIELDS = ["email"]

    def __str__(self) -> str:
        return self.name or self.email

    def save(self, *args, **kwargs):
        if self.phone_number:
            self.phone_number = self._normalize_phone_number(self.phone_number)
        return super().save(*args, **kwargs)

    @property
    def name(self):
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        elif self.first_name:
            return self.first_name
        else:
            return None

    def _normalize_phone_number(self, phone_number: str) -> str:
        return normalize_phone_number(phone_number)
