from datetime import timedelta

import pytest
from django.utils import timezone

from apps.domains.models import Domain
from apps.domains.tasks import send_domain_expiry_reminders, sync_domain_expiry_statuses


@pytest.mark.django_db
def test_send_domain_expiry_reminders_only_targets_matching_domains(django_user_model, monkeypatch):
    user = django_user_model.objects.create_user(email="expiry@example.com", password="password123")
    Domain.objects.create(
        user=user,
        name="soon.com",
        tld="com",
        status=Domain.STATUS_ACTIVE,
        expires_at=timezone.now().date() + timedelta(days=30),
    )
    Domain.objects.create(
        user=user,
        name="later.com",
        tld="com",
        status=Domain.STATUS_ACTIVE,
        expires_at=timezone.now().date() + timedelta(days=45),
    )
    sent = []

    def fake_send_notification(template_name, user, context=None):
        sent.append((template_name, user.email, context["domain"]))

    monkeypatch.setattr("apps.notifications.services.send_notification", fake_send_notification)

    count = send_domain_expiry_reminders(days_before=30)

    assert count == 1
    assert sent == [("domain_expiry_reminder", "expiry@example.com", "soon.com")]


@pytest.mark.django_db
def test_sync_domain_expiry_statuses_marks_past_domains_expired(django_user_model):
    user = django_user_model.objects.create_user(email="status@example.com", password="password123")
    expired_domain = Domain.objects.create(
        user=user,
        name="expired.com",
        tld="com",
        status=Domain.STATUS_ACTIVE,
        expires_at=timezone.now().date() - timedelta(days=1),
    )
    active_domain = Domain.objects.create(
        user=user,
        name="active.com",
        tld="com",
        status=Domain.STATUS_ACTIVE,
        expires_at=timezone.now().date() + timedelta(days=10),
    )

    updated = sync_domain_expiry_statuses()

    expired_domain.refresh_from_db()
    active_domain.refresh_from_db()
    assert updated == 1
    assert expired_domain.status == Domain.STATUS_EXPIRED
    assert active_domain.status == Domain.STATUS_ACTIVE
