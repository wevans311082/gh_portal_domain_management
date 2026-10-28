import importlib
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from apps.domains.models import PublicationRedaction, RegistrantPublicationDecision
from apps.domains.publication_consent import capture_decision
from tests.test_publication_redaction import redaction
from tests.test_contact_change_admission import current
from tests.test_contact_change_submission import api
from tests.test_registration_submission import order

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("state,expected", [("queued", "queued for a registrar check"),
    ("awaiting", "not confirmed until the registrar"), ("unknown", "without repeating the change"),
    ("review", "in-flight contact change needs review"), ("confirmed", "Public WHOIS/RDAP services may retain cached information")])
def test_customer_progress_separates_local_withdrawal_and_provider_confirmation(current, redaction, client, state, expected):
    job, api = redaction
    PublicationRedaction.objects.filter(pk=job.pk).update(status=state, confirmed_at=timezone.now() if state == "confirmed" else None,
        provider_scope="private-provider-scope")
    client.force_login(current[1].user)
    response = client.get(reverse("domains:publication_decisions"))
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    assert expected.encode() in response.content
    assert b"private-provider-scope" not in response.content


def test_redaction_progress_is_not_visible_to_other_customers(redaction, client, django_user_model):
    job, api = redaction
    other = django_user_model.objects.create_user(email="other-redaction@example.test", password="testpass")
    client.force_login(other)
    response = client.get(reverse("domains:publication_decisions"))
    assert job.decision.domain_name.encode() not in response.content
    assert b"Provider redaction:" not in response.content


def test_backfill_only_queues_tracked_withdrawals_and_preserves_existing_outcomes(current, redaction):
    job, api = redaction
    PublicationRedaction.objects.filter(pk=job.pk).update(status="confirmed")
    source = current[1]
    legacy = capture_decision(customer=source.user, contact=source.registration_contact, domain_name="legacy-withdrawal.com", choice="publish")
    active = capture_decision(customer=source.user, contact=source.registration_contact, domain_name="active-consent.com", choice="publish")
    local_only = capture_decision(customer=source.user, contact=source.registration_contact, domain_name="local-only.com", choice="publish")
    RegistrantPublicationDecision.objects.filter(pk=legacy.pk).update(withdrawn_at=timezone.now(), withdrawal_ticket_id=job.decision.withdrawal_ticket_id)
    RegistrantPublicationDecision.objects.filter(pk=active.pk).update(withdrawal_ticket_id=job.decision.withdrawal_ticket_id)
    RegistrantPublicationDecision.objects.filter(pk=local_only.pk).update(withdrawn_at=timezone.now())
    migration = importlib.import_module("apps.domains.migrations.0022_backfill_publication_redactions")
    migration.backfill_redactions(apps, SimpleNamespace(connection=connection))
    migration.backfill_redactions(apps, SimpleNamespace(connection=connection))
    assert PublicationRedaction.objects.get(decision=legacy).status == "queued"
    assert PublicationRedaction.objects.get(pk=job.pk).status == "confirmed"
    assert not PublicationRedaction.objects.filter(decision_id__in=[active.pk, local_only.pk]).exists()
    assert PublicationRedaction.objects.count() == 2
