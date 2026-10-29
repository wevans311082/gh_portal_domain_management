import uuid
import time
from unittest.mock import patch

import pytest
from django.core import signing
from django.test import Client
from django.urls import reverse

from apps.domains.models import RegistrarAgreementVersion, DomainAgreementAcceptance
from apps.domains.agreements import publish_agreement
from tests.security_helpers import login_owner
from tests.test_contact_change_recovery import owner
from tests.test_registrar_agreements import publication

pytestmark = pytest.mark.django_db


def url():
    return reverse("admin_tools:registrar_agreements")


def fields(**kwargs):
    data = dict(submission_key=str(uuid.uuid4()), agreement_key="domain-service", version="web-test-v1",
        title="Approved test agreement", body="Exact test text.\n<script>alert(123)</script>\n" + "unbroken-" * 40,
        source_url="https://example.test/terms", profile_keys=["general_raa", "uk_accuracy"],
        operations=["registration", "transfer"], action="preview")
    data.update(kwargs)
    return data


def preview(client, data):
    response = client.post(url(), data)
    assert response.status_code == 200
    return response.context["form"]["preview_token"].value()


def test_owner_must_preview_and_approve_exact_text_before_publication(owner, client):
    login_owner(client, owner)
    data = fields(reviewed="on")
    response = client.post(url(), data)
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    assert not response.context["form"]["reviewed"].value()
    assert b"&lt;script&gt;alert(123)&lt;/script&gt;" in response.content
    assert b"<script>alert(123)</script>" not in response.content
    assert not RegistrarAgreementVersion.objects.exists()
    data.update(action="publish", preview_token=response.context["form"]["preview_token"].value())
    first = client.post(url(), data)
    assert first.status_code == 302
    assert client.post(url(), data).status_code == 302
    document = RegistrarAgreementVersion.objects.get()
    assert document.body == data["body"]
    assert not DomainAgreementAcceptance.objects.exists()
    detail = client.get(first.url)
    assert detail.status_code == 200 and detail["Cache-Control"] == "no-store"
    assert b"Exact published text" in detail.content
    assert b"<script>alert(123)</script>" not in detail.content
    assert b"not legal certification or customer acceptance" in detail.content


def test_no_preview_token_or_missing_approval_cannot_publish(owner, client):
    login_owner(client, owner)
    data = fields(action="publish", reviewed="on")
    assert client.post(url(), data).status_code == 400
    data["action"] = "preview"
    token = preview(client, data)
    data.update(action="publish", preview_token=token, reviewed="")
    response = client.post(url(), data)
    assert response.status_code == 400 and "reviewed" in response.context["form"].errors
    assert not RegistrarAgreementVersion.objects.exists()


@pytest.mark.parametrize("change", [{"body": "Changed text"}, {"version": "another-v2"},
    {"profile_keys": ["us_nexus"]}, {"operations": ["registration"]}])
def test_changes_after_preview_require_another_preview(owner, client, change):
    login_owner(client, owner)
    data = fields()
    token = preview(client, data)
    data.update(action="publish", preview_token=token, reviewed="on", **change)
    response = client.post(url(), data)
    assert response.status_code == 400 and b"changed after preview" in response.content
    assert not RegistrarAgreementVersion.objects.exists()


def test_group_membership_changes_after_preview_also_require_review(owner, client, monkeypatch):
    from apps.domains.registrar_policy import PROFILE_TLDS
    login_owner(client, owner)
    data = fields()
    token = preview(client, data)
    policy, members = PROFILE_TLDS["general_raa"]
    monkeypatch.setitem(PROFILE_TLDS, "general_raa", (policy, members + ("new-reviewed-extension",)))
    response = client.post(url(), dict(data, action="publish", preview_token=token, reviewed="on"))
    assert response.status_code == 400 and not RegistrarAgreementVersion.objects.exists()


def test_another_publication_after_preview_cannot_be_silently_overwritten(owner, client):
    login_owner(client, owner)
    data = fields()
    token = preview(client, data)
    newer = publish_agreement(actor=owner, **publication(version="concurrent-v2"))
    response = client.post(url(), dict(data, action="publish", preview_token=token, reviewed="on"))
    assert response.status_code == 400 and b"assignments changed after preview" in response.content
    assert RegistrarAgreementVersion.objects.count() == 1
    assert newer.current_bindings.count() == 8


def test_preview_expires_without_expiring_the_owner_mfa_session(owner, client):
    login_owner(client, owner)
    data = fields()
    with patch.object(signing.TimestampSigner, "timestamp", return_value=signing.b62_encode(int(time.time()) - 1801)):
        token = preview(client, data)
    response = client.post(url(), dict(data, action="publish", preview_token=token, reviewed="on"))
    assert response.status_code == 400 and b"Preview the current text" in response.content
    assert not RegistrarAgreementVersion.objects.exists()


@pytest.mark.parametrize("values", [{"action": ["preview", "publish"]}, {"version": ["v1", "v2"]},
    {"profile_keys": ["general_raa", "general_raa"]}, {"unexpected": "value"}])
def test_ambiguous_or_unknown_form_fields_do_not_publish(owner, client, values):
    login_owner(client, owner)
    response = client.post(url(), fields(**values))
    assert response.status_code == 400 and not RegistrarAgreementVersion.objects.exists()


def test_ordinary_staff_and_owner_without_recent_mfa_cannot_read_or_publish(owner, client, django_user_model):
    staff = django_user_model.objects.create_user(email="agreement-staff@example.test", password="testpass", is_staff=True)
    client.force_login(staff)
    assert client.get(url()).status_code == 403
    assert client.post(url(), fields()).status_code == 403
    client.force_login(owner)
    assert client.get(url()).status_code in {302, 403}
    assert client.post(url(), fields()).status_code in {302, 403}
    assert not RegistrarAgreementVersion.objects.exists()


def test_preview_requires_csrf_and_published_detail_is_read_only(owner, client):
    login_owner(client, owner)
    strict = Client(enforce_csrf_checks=True)
    strict.cookies = client.cookies
    assert strict.post(url(), fields()).status_code == 403
    document = publish_agreement(actor=owner, **publication())
    detail = reverse("admin_tools:registrar_agreement_detail", args=[document.pk])
    assert client.post(detail).status_code == 405


def test_integrity_failure_preserves_escaped_text_and_withholds_unsafe_source_link(owner, client):
    login_owner(client, owner)
    document = publish_agreement(actor=owner, **publication())
    RegistrarAgreementVersion.objects.filter(pk=document.pk).update(source_url="javascript:alert(123)")
    response = client.get(reverse("admin_tools:registrar_agreement_detail", args=[document.pk]))
    assert b"Agreement record needs review" in response.content
    assert b'href="javascript:' not in response.content
    assert b"&lt;script&gt;plain text&lt;/script&gt;" in response.content


def test_body_is_bounded_before_publication(owner, client):
    login_owner(client, owner)
    response = client.post(url(), fields(body="x" * (256 * 1024)))
    assert response.status_code == 413 and not RegistrarAgreementVersion.objects.exists()
