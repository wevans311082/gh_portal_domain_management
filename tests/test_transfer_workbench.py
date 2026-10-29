from tests.agreement_helpers import attach_agreement
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.domains.models import DomainTransferAttempt, DomainTransfer, TLDPricing
from apps.portal.cart_service import add_domain_transfer_item, create_invoice_from_cart, CartPreflightError
from apps.billing.models import Invoice
from tests.test_transfer_submission import transfer, provider, submit

pytestmark = pytest.mark.django_db


def staff_login(client, django_user_model, role="Portal Support"):
    staff = django_user_model.objects.create_user(email="transfer-viewer@example.test", password="testpass", is_staff=True)
    staff.groups.add(Group.objects.get(name=role))
    client.force_login(staff)
    return staff


def test_workbench_is_readonly_and_excludes_secrets_financial_data_and_diagnostics(transfer, provider, client, django_user_model):
    attach_agreement(transfer)
    submit(transfer, provider)
    DomainTransfer.objects.filter(pk=transfer.pk).update(last_error="PRIVATE-PROVIDER-DIAGNOSTIC")
    staff_login(client, django_user_model)
    with patch("apps.domains.tasks.ResellerClubClient") as registrar:
        for name, args in (("domain_transfers_list", []), ("domain_transfer_detail", [transfer.pk])):
            url = reverse("admin_tools:" + name, args=args)
            response = client.get(url)
            assert response.status_code == 200 and response["Cache-Control"] == "no-store"
            html = response.content.decode()
            assert "PRIVATE-AUTH-CODE" not in html and "PRIVATE-PROVIDER-DIAGNOSTIC" not in html
            assert transfer.invoice.number not in html and transfer.user.email not in html
            assert client.post(url, {"action": "retry"}).status_code == 403
        registrar.assert_not_called()


def test_workbench_capability_is_rechecked_after_role_loss(transfer, client, django_user_model):
    staff = staff_login(client, django_user_model)
    assert client.get(reverse("admin_tools:domain_transfer_detail", args=[transfer.pk])).status_code == 200
    staff.groups.clear()
    assert client.get(reverse("admin_tools:domain_transfer_detail", args=[transfer.pk])).status_code == 403


def test_customer_cannot_read_staff_transfer_detail(transfer, client):
    client.force_login(transfer.user)
    response = client.get(reverse("admin_tools:domain_transfer_detail", args=[transfer.pk]))
    assert response.status_code == 302 and response.url.startswith("/accounts/login/")
    assert transfer.domain_name not in response.content.decode()


def test_customer_notice_is_owned_and_never_discloses_codes_or_old_errors(transfer, client, django_user_model):
    DomainTransferAttempt.objects.create(transfer=transfer, status="unknown", legacy_review_required=True)
    client.force_login(transfer.user)
    html = client.get(reverse("domains:my_domains")).content.decode()
    assert transfer.domain_name in html and "Support review required" in html and "PRIVATE-AUTH-CODE" not in html
    other = django_user_model.objects.create_user(email="transfer-outsider@example.test", password="testpass")
    client.force_login(other)
    assert transfer.domain_name not in client.get(reverse("domains:my_domains")).content.decode()


def test_failed_transfer_cannot_be_reinvoiced_as_a_new_request(transfer):
    transfer.status = "failed"
    transfer.save()
    TLDPricing.objects.create(tld="com", transfer_cost=12)
    item = add_domain_transfer_item(user=transfer.user, domain_name=transfer.domain_name,
        domain_contact_id=transfer.registration_contact_id)
    before = Invoice.objects.count()
    with pytest.raises(CartPreflightError, match="support review"):
        create_invoice_from_cart(item.cart)
    assert Invoice.objects.count() == before
