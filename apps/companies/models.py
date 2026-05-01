from django.db import models
from apps.core.models import TimeStampedModel
from apps.accounts.models import User


class BusinessProfile(TimeStampedModel):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="business_profile")
    company_name = models.CharField(max_length=255)
    company_number = models.CharField(max_length=20, blank=True)
    company_type = models.CharField(max_length=50, blank=True)
    registered_address = models.TextField(blank=True)
    status = models.CharField(max_length=50, blank=True)
    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    companies_house_data = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.company_name} ({self.user.email})"
