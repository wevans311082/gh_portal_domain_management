import pytest
from django.urls import reverse

from apps.domains.models import DomainContact, DomainOrder


@pytest.mark.django_db
def test_domain_orders_list_open_filter(client, django_user_model):
    staff = django_user_model.objects.create_user(email="s@example.com", password="x", is_staff=True)
    user = django_user_model.objects.create_user(email="u@example.com", password="x")
    client.force_login(staff)
    contact = DomainContact.objects.create(
        user=user,
        label="P",
        name="U",
        email=user.email,
        phone="07",
        address_line1="1",
        city="L",
        state="L",
        postcode="E1",
        country="GB",
    )
    DomainOrder.objects.create(
        user=user,
        domain_name="open1.com",
        tld="com",
        status=DomainOrder.STATUS_FAILED,
        registration_contact=contact,
        admin_contact=contact,
        tech_contact=contact,
        billing_contact=contact,
    )
    DomainOrder.objects.create(
        user=user,
        domain_name="done1.com",
        tld="com",
        status=DomainOrder.STATUS_COMPLETED,
        registration_contact=contact,
        admin_contact=contact,
        tech_contact=contact,
        billing_contact=contact,
    )
    resp = client.get(reverse("admin_tools:domain_orders_list"))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "open1.com" in body
    assert "done1.com" not in body


@pytest.mark.django_db
def test_domain_order_cancel_and_delete(client, django_user_model):
    staff = django_user_model.objects.create_user(email="s2@example.com", password="x", is_staff=True)
    user = django_user_model.objects.create_user(email="u2@example.com", password="x")
    client.force_login(staff)
    contact = DomainContact.objects.create(
        user=user,
        label="P",
        name="U",
        email=user.email,
        phone="07",
        address_line1="1",
        city="L",
        state="L",
        postcode="E1",
        country="GB",
    )
    order = DomainOrder.objects.create(
        user=user,
        domain_name="stuck.com",
        tld="com",
        status=DomainOrder.STATUS_PAID,
        registration_contact=contact,
        admin_contact=contact,
        tech_contact=contact,
        billing_contact=contact,
    )
    resp = client.post(reverse("admin_tools:domain_order_action", args=[order.pk, "pause"]), {"reason": "wait"})
    assert resp.status_code == 302
    order.refresh_from_db()
    assert order.status == DomainOrder.STATUS_PAUSED

    resp = client.post(reverse("admin_tools:domain_order_action", args=[order.pk, "cancel"]), {"reason": "nope"})
    assert resp.status_code == 302
    order.refresh_from_db()
    assert order.status == DomainOrder.STATUS_CANCELLED

    resp = client.post(reverse("admin_tools:domain_order_action", args=[order.pk, "delete"]))
    assert resp.status_code == 302
    assert not DomainOrder.objects.filter(pk=order.pk).exists()
