from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
import uuid

import pytest
from django.contrib.auth.models import Group, Permission
from django.db import DatabaseError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from allauth.account.models import EmailAddress
from apps.accounts.models import ClientProfile
from apps.admin_tools.models import CustomerNote
from apps.audit.models import AuditLog
from apps.billing.models import Invoice
from apps.domains.models import Domain, DomainContact
from apps.support.models import SupportTicket
from tests.security_helpers import login_owner

pytestmark = pytest.mark.django_db


@pytest.fixture
def customer(django_user_model):
    return django_user_model.objects.create_user(email="customer360@example.test", password="testpass", first_name="Jane", last_name="Smith")


@pytest.fixture
def staff(client, django_user_model):
    user = django_user_model.objects.create_user(email="workspace-staff@example.test", password="testpass", is_staff=True)
    user.groups.add(Group.objects.get(name="Portal Support"))
    client.force_login(user)
    return user


def workspace(customer):
    return reverse("admin_tools:customer_workspace", args=[customer.pk])


def add_url(customer):
    return reverse("admin_tools:customer_note_add", args=[customer.pk])


def grant(staff, *permissions):
    staff.user_permissions.add(*Permission.objects.filter(content_type__app_label="admin_tools", codename__in=permissions))


def test_support_workspace_omits_financial_and_security_panels(client, customer, staff):
    Invoice.objects.create(user=customer, number="SECRET-FINANCE", total=Decimal("4321"))
    Domain.objects.create(user=customer, name="owned.test", tld="test")
    SupportTicket.objects.create(user=customer, subject="Need assistance")
    response = client.get(workspace(customer))
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    html = response.content.decode()
    assert "owned.test" in html and "Need assistance" in html
    assert "SECRET-FINANCE" not in html
    assert "Outstanding balances" not in html
    assert "Multi-factor authentication" not in html
    assert "Add internal note" in html
    assert "No saved domain contacts" in html


def test_billing_workspace_respects_role_and_keeps_currency_balances_separate(client, customer, staff):
    staff.groups.clear()
    staff.groups.add(Group.objects.get(name="Portal Billing"))
    Invoice.objects.create(user=customer, number="GBP-1", currency="GBP", total=100, amount_paid=25, status="partially_paid")
    Invoice.objects.create(user=customer, number="EUR-1", currency="EUR", total=50, status="overdue")
    Invoice.objects.create(user=customer, number="VOID-1", currency="GBP", total=99999, status="void")
    Invoice.objects.create(user=customer, number="DRAFT-1", currency="GBP", total=99999, status="draft")
    Domain.objects.create(user=customer, name="secret-domain.example", tld="example")
    response = client.get(workspace(customer))
    assert [(row["currency"], row["outstanding"]) for row in response.context["balances"]] == [("EUR", Decimal("50")), ("GBP", Decimal("75"))]
    html = response.content.decode()
    assert "secret-domain.example" not in html and "Add internal note" not in html
    assert "Contact validation" not in html and "Multi-factor authentication" not in html


def test_unauthorized_customer_and_privileged_accounts_cannot_be_opened(client, customer, staff, django_user_model):
    staff.groups.clear()
    assert client.get(workspace(customer)).status_code == 403
    staff.groups.add(Group.objects.get(name="Portal Support"))
    privileged = django_user_model.objects.create_user(email="privileged@example.test", password="testpass", is_staff=True)
    assert client.get(workspace(privileged)).status_code == 404
    privileged.is_staff = False
    privileged.is_superuser = True
    privileged.save()
    assert client.get(workspace(privileged)).status_code == 404


def test_individual_completeness_does_not_become_identity_or_business_verification(client, customer, staff):
    ClientProfile.objects.create(user=customer, is_business=False)
    EmailAddress.objects.create(user=customer, email=customer.email, verified=True)
    DomainContact.objects.create(user=customer, label="Home", registrant_type="individual", name="Jane Smith", email=customer.email,
        address_line1="1 High Street", city="London", state="London", postcode="SW1A 1AA", country="GB", phone_country_code="44", phone="07700900000")
    with patch("apps.domains.resellerclub_client.ResellerClubClient") as provider:
        response = client.get(workspace(customer))
    provider.assert_not_called()
    assert response.context["portal_email_verified"]
    assert response.context["contact_checks"][0]["business"] == "Not applicable"
    assert response.context["contact_checks"][0]["data"] == "Complete for general contact fields"
    assert "Saving details does not verify identity" in response.content.decode()


def test_workspace_shows_independent_evidence_status_without_private_reference(client, customer, staff):
    from apps.domains.models import ContactVerification
    contact = DomainContact.objects.create(user=customer, label="Reviewed individual", registrant_type="individual", name="Jane Smith")
    for kind, tld in (("identity", ""), ("eligibility", "com")):
        ContactVerification.objects.create(contact=contact, check_kind=kind, tld=tld, status="verified", source="local-policy",
            reference="PRIVATE-IDENTITY-CASE", subject_digest=ContactVerification.digest_for(contact), checked_at=timezone.now())
    response = client.get(workspace(customer))
    check = response.context["contact_checks"][0]
    assert check["identity"] == "Verified" and check["business"] == "Not applicable"
    assert check["eligibility"][0]["label"] == "Verified"
    assert b"Identity review" in response.content and b"contact-level evidence; no domain association confirmed" in response.content
    assert b"PRIVATE-IDENTITY-CASE" not in response.content


def test_panels_and_timeline_never_include_another_customers_records(client, customer, staff, django_user_model):
    other = django_user_model.objects.create_user(email="other360@example.test", password="testpass")
    Domain.objects.create(user=other, name="OTHER-PRIVATE.example", tld="example")
    SupportTicket.objects.create(user=other, subject="OTHER-PRIVATE-SUPPORT")
    CustomerNote.objects.create(customer=other, author=staff, body="OTHER-PRIVATE-NOTE", submission_key=uuid.uuid4())
    response = client.get(workspace(customer))
    assert b"OTHER-PRIVATE" not in response.content


def test_domain_panel_pagination_and_role_revocation(client, customer, staff):
    Domain.objects.bulk_create([Domain(user=customer, name=f"asset-{i}.example", tld="example") for i in range(25)])
    response = client.get(workspace(customer))
    panel = next(panel for panel in response.context["panels"] if panel["key"] == "domains")
    assert panel["count"] == 25 and len(panel["items"]) == 5
    response = client.get(workspace(customer), {"section": "domains", "page": 2})
    panel = next(panel for panel in response.context["panels"] if panel["key"] == "domains")
    assert len(panel["items"]) == 5 and panel["page"].number == 2
    staff.groups.clear()
    grant(staff, "view_customers")
    assert client.get(workspace(customer), {"section": "domains"}).status_code == 404
    assert client.get(workspace(customer), {"page": "9" * 100, "section": "invoices"}).status_code == 404


def test_timeline_cursor_ties_are_stable_scoped_and_tamper_resistant(client, customer, staff, django_user_model):
    Domain.objects.bulk_create([Domain(user=customer, name=f"timeline-{i}.example", tld="example") for i in range(35)])
    stamp = timezone.now() - timedelta(days=1)
    Domain.objects.filter(user=customer).update(created_at=stamp, updated_at=stamp)
    response = client.get(workspace(customer))
    first = response.context["activity"]
    token = response.context["next_cursor"]
    assert len(first) == 20 and token
    response = client.get(workspace(customer), {"cursor": token})
    second = response.context["activity"]
    assert not {(e["source"], e["pk"]) for e in first} & {(e["source"], e["pk"]) for e in second}
    assert len(first) + len(second) == 36
    response = client.get(workspace(customer), {"cursor": token + "broken"})
    assert "invalid" in response.context["activity_error"]
    other = django_user_model.objects.create_user(email="cursor-other@example.test", password="testpass")
    assert "invalid" in client.get(workspace(other), {"cursor": token}).context["activity_error"]


def test_notes_are_idempotent_escaped_and_audited_without_text(client, customer, staff):
    key = uuid.uuid4()
    body = '<script>alert("private note")</script>\nSecond line'
    for _ in range(2):
        assert client.post(add_url(customer), {"submission_key": key, "body": body}).status_code == 302
    assert CustomerNote.objects.count() == 1
    assert AuditLog.objects.filter(action="customer.note.added").count() == 1
    assert "private note" not in str(AuditLog.objects.get(action="customer.note.added").data)
    html = client.get(workspace(customer)).content.decode()
    assert "&lt;script&gt;" in html and body not in html
    note = CustomerNote.objects.get()
    assert client.get(reverse("admin_tools:customer_note", args=[customer.pk, note.pk])).status_code == 200
    client.force_login(customer)
    portal_response = client.get(reverse("portal:dashboard"))
    assert portal_response.status_code == 200 and b"private note" not in portal_response.content
    assert client.get(workspace(customer)).status_code == 302
    assert client.get(reverse("admin_tools:customer_note", args=[customer.pk, note.pk])).status_code == 302


def test_role_revocation_hides_previously_visible_timeline_sources(client, customer, staff):
    grant(staff, "view_billing")
    Invoice.objects.create(user=customer, number="REVOKED-FINANCIAL-RECORD")
    assert b"REVOKED-FINANCIAL-RECORD" in client.get(workspace(customer)).content
    staff.user_permissions.clear()
    assert b"REVOKED-FINANCIAL-RECORD" not in client.get(workspace(customer)).content


def test_owner_security_panel_never_exposes_mfa_secret(client, customer, staff):
    grant(staff, "view_audit")
    customer.mfa_enabled = True
    customer.mfa_secret = "NEVER-EXPOSE-THIS-SECRET"
    customer.save()
    response = client.get(workspace(customer))
    assert b"Multi-factor authentication" in response.content
    assert b"NEVER-EXPOSE-THIS-SECRET" not in response.content


def test_note_submission_keys_cannot_cross_customers_or_change_content(client, customer, staff, django_user_model):
    key = uuid.uuid4()
    assert client.post(add_url(customer), {"submission_key": key, "body": "Original note"}).status_code == 302
    assert client.post(add_url(customer), {"submission_key": key, "body": "Changed note"}).status_code == 400
    other = django_user_model.objects.create_user(email="other-note@example.test", password="testpass")
    assert client.post(add_url(other), {"submission_key": key, "body": "Original note"}).status_code == 400
    assert CustomerNote.objects.count() == 1


def test_restricted_notes_are_filtered_from_panels_timeline_counts_and_direct_links(client, customer, staff):
    billing = CustomerNote.objects.create(customer=customer, author=staff, body="BILLING-ONLY-TEXT", audience="billing", submission_key=uuid.uuid4())
    security = CustomerNote.objects.create(customer=customer, author=staff, body="SECURITY-ONLY-TEXT", audience="security", submission_key=uuid.uuid4())
    response = client.get(workspace(customer))
    assert response.context["notes"].paginator.count == 0
    assert all(event["source"] != "notes:created_at" for event in response.context["activity"])
    for note in (billing, security):
        assert client.get(reverse("admin_tools:customer_note", args=[customer.pk, note.pk])).status_code == 404
    grant(staff, "view_billing")
    html = client.get(workspace(customer)).content.decode()
    assert "BILLING-ONLY-TEXT" in html and "SECURITY-ONLY-TEXT" not in html
    grant(staff, "view_audit")
    assert b"SECURITY-ONLY-TEXT" in client.get(workspace(customer)).content


@pytest.mark.parametrize("audience", ["billing", "security", "made-up"])
def test_support_cannot_forge_restricted_note_audiences(client, customer, staff, audience):
    response = client.post(add_url(customer), {"submission_key": uuid.uuid4(), "body": "Restricted note", "audience": audience})
    assert response.status_code == 400 and b'id_audience' in response.content
    assert not CustomerNote.objects.exists()


def test_billing_note_requires_both_note_and_billing_management_permissions(client, customer, staff):
    grant(staff, "manage_billing", "view_billing")
    assert client.post(add_url(customer), {"submission_key": uuid.uuid4(), "body": "Billing service context", "audience": "billing"}).status_code == 302
    note = CustomerNote.objects.get()
    assert note.audience == "billing"
    staff.user_permissions.clear()
    assert client.get(reverse("admin_tools:customer_note", args=[customer.pk, note.pk])).status_code == 404


def test_security_note_requires_recent_owner_mfa(client, customer, staff, django_user_model):
    owner = django_user_model.objects.create_user(email="security-note-owner@example.test", password="testpass")
    client.logout()
    login_owner(client, owner)
    payload = {"submission_key": uuid.uuid4(), "body": "Reviewed account security context", "audience": "security"}
    assert client.post(add_url(customer), payload).status_code == 302
    assert CustomerNote.objects.get().audience == "security"
    session = client.session
    session["_mfa_verified_at"] = 0
    session.save()
    payload["submission_key"] = uuid.uuid4()
    assert client.post(add_url(customer), payload).status_code == 403
    assert CustomerNote.objects.count() == 1


@pytest.mark.parametrize("body,key", [("", uuid.uuid4()), ("x" * 4001, uuid.uuid4()), ("bad\x01note", uuid.uuid4()), ("Text", "broken")])
def test_invalid_notes_display_linked_errors_and_make_no_changes(client, customer, staff, body, key):
    response = client.post(add_url(customer), {"body": body, "submission_key": key})
    assert response.status_code == 400
    assert b"Check the highlighted fields" in response.content
    assert not CustomerNote.objects.exists()


def test_billing_and_read_only_staff_cannot_write_notes(client, customer, staff):
    staff.groups.clear()
    staff.groups.add(Group.objects.get(name="Portal Billing"))
    assert client.post(add_url(customer), {"body": "Forbidden", "submission_key": uuid.uuid4()}).status_code == 403
    staff.groups.clear()
    grant(staff, "manage_customer_notes")
    assert client.post(add_url(customer), {"body": "Forbidden", "submission_key": uuid.uuid4()}).status_code == 403
    assert not CustomerNote.objects.exists()


def test_note_audit_failure_rolls_back_text(client, customer, staff):
    with patch("apps.admin_tools.customer_workspace.AuditLog.objects.create", side_effect=DatabaseError("unavailable")):
        with pytest.raises(DatabaseError):
            client.post(add_url(customer), {"body": "Must roll back", "submission_key": uuid.uuid4()})
    assert not CustomerNote.objects.exists()


def test_timeline_uses_owner_timestamp_indexes_and_bounded_queries(client, customer, staff):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    Domain.objects.bulk_create([Domain(user=customer, name=f"indexed-{i}.example", tld="example") for i in range(60)])
    with CaptureQueriesContext(connection) as captured:
        response = client.get(workspace(customer))
    assert response.status_code == 200
    for field, name in [("created_at", "workspace_domain_created"), ("updated_at", "workspace_domain_updated")]:
        sql = next(query["sql"] for query in captured if 'FROM "domains_domain"' in query["sql"] and f'AS "{field}"' in query["sql"].partition(' FROM ')[0] and "LIMIT 21" in query["sql"])
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN QUERY PLAN " + sql)
            plan = " ".join(str(row) for row in cursor.fetchall())
        assert name in plan


def test_redaction_audit_failure_preserves_original_note(client, customer, staff, django_user_model):
    note = CustomerNote.objects.create(customer=customer, author=staff, body="Retain until audited", submission_key=uuid.uuid4())
    owner = django_user_model.objects.create_user(email="redact-rollback@example.test", password="testpass")
    client.logout()
    login_owner(client, owner)
    with patch("apps.admin_tools.customer_workspace.AuditLog.objects.create", side_effect=DatabaseError("unavailable")):
        with pytest.raises(DatabaseError):
            client.post(reverse("admin_tools:customer_note_redact", args=[customer.pk, note.pk]))
    note.refresh_from_db()
    assert note.body == "Retain until audited" and note.redacted_at is None


def test_notes_require_csrf_and_bounded_post_body(client, customer, staff):
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(staff)
    assert csrf_client.post(add_url(customer), {"body": "CSRF", "submission_key": uuid.uuid4()}).status_code == 403
    assert client.post(add_url(customer), data="x" * (65536 + 1), content_type="application/x-www-form-urlencoded").status_code == 413
    assert not CustomerNote.objects.exists()


def test_maximum_unicode_note_fits_the_encoded_form_request_limit(client, customer, staff):
    from urllib.parse import urlencode

    body = "\U0001f642" * 4000
    payload = urlencode({"body": body, "submission_key": str(uuid.uuid4()), "audience": "general"})
    assert client.post(add_url(customer), data=payload, content_type="application/x-www-form-urlencoded").status_code == 302
    assert CustomerNote.objects.get().body == body


def test_note_writes_fail_closed_when_shared_limits_are_unavailable(client, customer, staff):
    from apps.core.abuse import AbuseStoreUnavailable

    with patch("apps.core.abuse.consume_limit", side_effect=AbuseStoreUnavailable):
        response = client.post(add_url(customer), {"body": "No write without limits", "submission_key": uuid.uuid4()})
    assert response.status_code == 503 and not CustomerNote.objects.exists()


def test_only_recently_authenticated_owner_can_redact_and_replay_is_safe(client, customer, staff, django_user_model):
    note = CustomerNote.objects.create(customer=customer, author=staff, body="Sensitive text to remove", submission_key=uuid.uuid4())
    url = reverse("admin_tools:customer_note_redact", args=[customer.pk, note.pk])
    assert client.post(url).status_code == 403
    owner = django_user_model.objects.create_user(email="workspace-owner@example.test", password="testpass")
    client.logout()
    login_owner(client, owner)
    assert client.post(url).status_code == 302
    assert client.post(url).status_code == 302
    note.refresh_from_db()
    assert note.body == "" and note.redacted_by_id == owner.pk and note.redacted_at
    assert AuditLog.objects.filter(action="customer.note.redacted").count() == 1
    assert b"Sensitive text to remove" not in client.get(workspace(customer)).content
    assert client.post(add_url(customer), {"body": "Sensitive text to remove", "submission_key": note.submission_key}).status_code == 400
