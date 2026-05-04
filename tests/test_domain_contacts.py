import pytest
from django.urls import reverse

from apps.domains.models import DomainContact, Domain


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def make_user(django_user_model, email="contact@example.com"):
    return django_user_model.objects.create_user(email=email, password="testpass123")


VALID_PAYLOAD = {
    "label": "Primary",
    "name": "Jane Smith",
    "company": "",
    "email": "jane@example.com",
    "phone_country_code": "44",
    "phone": "07700900000",
    "address_line1": "1 High Street",
    "address_line2": "",
    "city": "London",
    "state": "Greater London",
    "postcode": "SW1A 1AA",
    "country": "GB",
    "is_default": True,
}


def make_contact(user, label="Primary", is_default=True):
    return DomainContact.objects.create(
        user=user,
        label=label,
        name="Jane Smith",
        email="jane@example.com",
        phone_country_code="44",
        phone="07700900000",
        address_line1="1 High Street",
        city="London",
        state="Greater London",
        postcode="SW1A 1AA",
        country="GB",
        is_default=is_default,
    )


# ─────────────────────────────────────────────
# contact_list
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_contact_list_requires_login(client):
    url = reverse("domains:contact_list")
    assert client.get(url).status_code == 302


@pytest.mark.django_db
def test_contact_list_empty(client, django_user_model):
    user = make_user(django_user_model)
    client.force_login(user)
    response = client.get(reverse("domains:contact_list"))
    assert response.status_code == 200
    assert b"No contacts yet" in response.content


@pytest.mark.django_db
def test_contact_list_shows_own_contacts(client, django_user_model):
    user = make_user(django_user_model)
    make_contact(user, label="Home")
    other = django_user_model.objects.create_user(email="other@example.com", password="pass")
    make_contact(other, label="Work")
    client.force_login(user)
    response = client.get(reverse("domains:contact_list"))
    assert b"Home" in response.content
    assert b"Work" not in response.content


# ─────────────────────────────────────────────
# contact_create
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_contact_create_get(client, django_user_model):
    user = make_user(django_user_model)
    client.force_login(user)
    response = client.get(reverse("domains:contact_create"))
    assert response.status_code == 200
    assert b"Create Contact" in response.content


@pytest.mark.django_db
def test_contact_create_post_valid(client, django_user_model):
    user = make_user(django_user_model)
    client.force_login(user)
    response = client.post(reverse("domains:contact_create"), VALID_PAYLOAD)
    assert response.status_code == 302
    assert DomainContact.objects.filter(user=user, label="Primary").exists()


@pytest.mark.django_db
def test_contact_create_invalid_country(client, django_user_model):
    user = make_user(django_user_model)
    client.force_login(user)
    # Django's max_length=2 fires before clean_country; form re-renders with an error
    bad = dict(VALID_PAYLOAD, country="GREAT_BRITAIN")
    response = client.post(reverse("domains:contact_create"), bad)
    assert response.status_code == 200
    assert not DomainContact.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_contact_create_sets_only_one_default(client, django_user_model):
    user = make_user(django_user_model)
    make_contact(user, label="Old Default", is_default=True)
    client.force_login(user)
    payload = dict(VALID_PAYLOAD, label="New Default", is_default=True)
    client.post(reverse("domains:contact_create"), payload)
    defaults = DomainContact.objects.filter(user=user, is_default=True)
    assert defaults.count() == 1
    assert defaults.first().label == "New Default"


@pytest.mark.django_db
def test_contact_create_prefills_from_profile(client, django_user_model):
    """When no contacts exist, GET should pre-fill from ClientProfile."""
    from apps.accounts.models import ClientProfile
    user = make_user(django_user_model)
    ClientProfile.objects.create(
        user=user,
        address_line1="99 Test Road",
        city="Manchester",
        county="Greater Manchester",
        postcode="M1 1AA",
        country="GB",
    )
    client.force_login(user)
    response = client.get(reverse("domains:contact_create"))
    assert b"99 Test Road" in response.content


# ─────────────────────────────────────────────
# contact_edit
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_contact_edit_get(client, django_user_model):
    user = make_user(django_user_model)
    contact = make_contact(user)
    client.force_login(user)
    url = reverse("domains:contact_edit", kwargs={"pk": contact.pk})
    response = client.get(url)
    assert response.status_code == 200
    assert b"Edit Contact" in response.content


@pytest.mark.django_db
def test_contact_edit_updates_name(client, django_user_model):
    user = make_user(django_user_model)
    contact = make_contact(user)
    client.force_login(user)
    url = reverse("domains:contact_edit", kwargs={"pk": contact.pk})
    payload = dict(VALID_PAYLOAD, name="Updated Name")
    client.post(url, payload)
    contact.refresh_from_db()
    assert contact.name == "Updated Name"


@pytest.mark.django_db
def test_contact_edit_rejects_wrong_user(client, django_user_model):
    user = make_user(django_user_model)
    contact = make_contact(user)
    other = django_user_model.objects.create_user(email="other2@example.com", password="pass")
    client.force_login(other)
    url = reverse("domains:contact_edit", kwargs={"pk": contact.pk})
    assert client.get(url).status_code == 404


@pytest.mark.django_db
def test_contact_edit_promoting_to_default_demotes_old(client, django_user_model):
    user = make_user(django_user_model)
    old_default = make_contact(user, label="Old Default", is_default=True)
    new_contact = make_contact(user, label="New", is_default=False)
    client.force_login(user)
    url = reverse("domains:contact_edit", kwargs={"pk": new_contact.pk})
    payload = dict(VALID_PAYLOAD, label="New", is_default=True)
    client.post(url, payload)
    old_default.refresh_from_db()
    new_contact.refresh_from_db()
    assert not old_default.is_default
    assert new_contact.is_default


# ─────────────────────────────────────────────
# contact_delete
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_contact_delete_removes_contact(client, django_user_model):
    user = make_user(django_user_model)
    contact = make_contact(user)
    client.force_login(user)
    url = reverse("domains:contact_delete", kwargs={"pk": contact.pk})
    response = client.post(url)
    assert response.status_code == 302
    assert not DomainContact.objects.filter(pk=contact.pk).exists()


@pytest.mark.django_db
def test_contact_delete_requires_post(client, django_user_model):
    user = make_user(django_user_model)
    contact = make_contact(user)
    client.force_login(user)
    url = reverse("domains:contact_delete", kwargs={"pk": contact.pk})
    assert client.get(url).status_code == 405


@pytest.mark.django_db
def test_contact_delete_rejects_wrong_user(client, django_user_model):
    user = make_user(django_user_model)
    contact = make_contact(user)
    other = django_user_model.objects.create_user(email="other3@example.com", password="pass")
    client.force_login(other)
    url = reverse("domains:contact_delete", kwargs={"pk": contact.pk})
    response = client.post(url)
    assert response.status_code == 404
    assert DomainContact.objects.filter(pk=contact.pk).exists()


@pytest.mark.django_db
def test_contact_delete_blocked_if_in_use(client, django_user_model):
    """A contact attached to a domain order cannot be deleted."""
    from apps.domains.models import DomainOrder
    user = make_user(django_user_model)
    contact = make_contact(user)
    DomainOrder.objects.create(
        user=user,
        domain_name="blocked.com",
        tld="com",
        registration_contact=contact,
        admin_contact=contact,
        tech_contact=contact,
        billing_contact=contact,
        status=DomainOrder.STATUS_COMPLETED,
    )
    client.force_login(user)
    url = reverse("domains:contact_delete", kwargs={"pk": contact.pk})
    response = client.post(url)
    assert response.status_code == 302
    assert DomainContact.objects.filter(pk=contact.pk).exists()


# ─────────────────────────────────────────────
# contact_set_default
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_contact_set_default(client, django_user_model):
    user = make_user(django_user_model)
    old = make_contact(user, label="Old", is_default=True)
    new = make_contact(user, label="New", is_default=False)
    client.force_login(user)
    url = reverse("domains:contact_set_default", kwargs={"pk": new.pk})
    response = client.post(url)
    assert response.status_code == 302
    old.refresh_from_db()
    new.refresh_from_db()
    assert not old.is_default
    assert new.is_default


@pytest.mark.django_db
def test_contact_set_default_requires_post(client, django_user_model):
    user = make_user(django_user_model)
    contact = make_contact(user)
    client.force_login(user)
    url = reverse("domains:contact_set_default", kwargs={"pk": contact.pk})
    assert client.get(url).status_code == 405


@pytest.mark.django_db
def test_contact_create_individual_auto_validates_registrant(client, django_user_model):
    user = make_user(django_user_model)
    client.force_login(user)
    payload = dict(VALID_PAYLOAD, company="", company_number="")
    client.post(reverse("domains:contact_create"), payload)

    contact = DomainContact.objects.get(user=user, label="Primary")
    assert contact.registrant_validation_status == DomainContact.VALIDATION_VALIDATED


@pytest.mark.django_db
def test_contact_create_company_without_number_rejected(client, django_user_model):
    user = make_user(django_user_model)
    client.force_login(user)
    payload = dict(VALID_PAYLOAD, company="Example Ltd", company_number="")
    response = client.post(reverse("domains:contact_create"), payload)

    assert response.status_code == 302
    contact = DomainContact.objects.get(user=user, label="Primary")
    assert contact.registrant_validation_status == DomainContact.VALIDATION_REJECTED
