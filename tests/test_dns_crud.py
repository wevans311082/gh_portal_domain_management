import pytest
from django.urls import reverse

from apps.dns.models import DNSZone, DNSRecord
from apps.domains.models import Domain


def make_domain(django_user_model, email="dnsuser@example.com"):
    user = django_user_model.objects.create_user(
        email=email,
        password="testpass123",
    )
    domain = Domain.objects.create(
        user=user,
        name="example.com",
        tld="com",
        status=Domain.STATUS_ACTIVE,
    )
    return user, domain


def make_zone(domain, provider="cloudflare"):
    return DNSZone.objects.create(domain=domain, provider=provider, is_active=True)


def make_record(zone, record_type="A", name="@", content="1.2.3.4"):
    return DNSRecord.objects.create(
        zone=zone,
        record_type=record_type,
        name=name,
        content=content,
        ttl=3600,
        is_active=True,
    )


# ──────────────────────────────────────────────
# zone_detail
# ──────────────────────────────────────────────

@pytest.mark.django_db
def test_zone_detail_requires_login(client):
    url = reverse("dns:zone_detail", kwargs={"domain_pk": 9999})
    response = client.get(url)
    assert response.status_code == 302
    assert "/login" in response["Location"] or "/accounts" in response["Location"]


@pytest.mark.django_db
def test_zone_detail_no_zone(client, django_user_model):
    user, domain = make_domain(django_user_model)
    client.force_login(user)
    url = reverse("dns:zone_detail", kwargs={"domain_pk": domain.pk})
    response = client.get(url)
    assert response.status_code == 200
    assert b"No DNS zone configured" in response.content


@pytest.mark.django_db
def test_zone_detail_shows_records(client, django_user_model):
    user, domain = make_domain(django_user_model)
    zone = make_zone(domain)
    make_record(zone, record_type="A", name="@", content="1.2.3.4")
    make_record(zone, record_type="MX", name="@", content="mail.example.com")
    client.force_login(user)
    url = reverse("dns:zone_detail", kwargs={"domain_pk": domain.pk})
    response = client.get(url)
    assert response.status_code == 200
    assert b"1.2.3.4" in response.content
    assert b"mail.example.com" in response.content


@pytest.mark.django_db
def test_zone_detail_only_shows_active_records(client, django_user_model):
    user, domain = make_domain(django_user_model)
    zone = make_zone(domain)
    make_record(zone, content="1.2.3.4")
    DNSRecord.objects.create(zone=zone, record_type="A", name="old", content="9.9.9.9", ttl=3600, is_active=False)
    client.force_login(user)
    url = reverse("dns:zone_detail", kwargs={"domain_pk": domain.pk})
    response = client.get(url)
    assert b"1.2.3.4" in response.content
    assert b"9.9.9.9" not in response.content


@pytest.mark.django_db
def test_zone_detail_rejects_other_users_domain(client, django_user_model):
    user, domain = make_domain(django_user_model)
    other = django_user_model.objects.create_user(email="other@example.com", password="pass")
    client.force_login(other)
    url = reverse("dns:zone_detail", kwargs={"domain_pk": domain.pk})
    response = client.get(url)
    assert response.status_code == 404


# ──────────────────────────────────────────────
# record_add
# ──────────────────────────────────────────────

@pytest.mark.django_db
def test_record_add_get(client, django_user_model):
    user, domain = make_domain(django_user_model)
    make_zone(domain)
    client.force_login(user)
    url = reverse("dns:record_add", kwargs={"domain_pk": domain.pk})
    response = client.get(url)
    assert response.status_code == 200
    assert b"Add DNS Record" in response.content


@pytest.mark.django_db
def test_record_add_creates_record(client, django_user_model):
    user, domain = make_domain(django_user_model)
    make_zone(domain)
    client.force_login(user)
    url = reverse("dns:record_add", kwargs={"domain_pk": domain.pk})
    response = client.post(url, {
        "record_type": "A",
        "name": "www",
        "content": "1.2.3.4",
        "ttl": 3600,
        "priority": "",
        "proxied": "",
    })
    assert response.status_code == 302
    assert DNSRecord.objects.filter(name="www", content="1.2.3.4").exists()


@pytest.mark.django_db
def test_record_add_mx_requires_priority(client, django_user_model):
    user, domain = make_domain(django_user_model)
    make_zone(domain)
    client.force_login(user)
    url = reverse("dns:record_add", kwargs={"domain_pk": domain.pk})
    response = client.post(url, {
        "record_type": "MX",
        "name": "@",
        "content": "mail.example.com",
        "ttl": 3600,
        "priority": "",
        "proxied": "",
    })
    assert response.status_code == 200
    assert b"Priority is required" in response.content


@pytest.mark.django_db
def test_record_add_mx_with_priority_succeeds(client, django_user_model):
    user, domain = make_domain(django_user_model)
    make_zone(domain)
    client.force_login(user)
    url = reverse("dns:record_add", kwargs={"domain_pk": domain.pk})
    response = client.post(url, {
        "record_type": "MX",
        "name": "@",
        "content": "mail.example.com",
        "ttl": 3600,
        "priority": 10,
        "proxied": "",
    })
    assert response.status_code == 302
    record = DNSRecord.objects.get(record_type="MX")
    assert record.priority == 10


# ──────────────────────────────────────────────
# record_edit
# ──────────────────────────────────────────────

@pytest.mark.django_db
def test_record_edit_get(client, django_user_model):
    user, domain = make_domain(django_user_model)
    zone = make_zone(domain)
    record = make_record(zone)
    client.force_login(user)
    url = reverse("dns:record_edit", kwargs={"domain_pk": domain.pk, "record_pk": record.pk})
    response = client.get(url)
    assert response.status_code == 200
    assert b"Edit DNS Record" in response.content


@pytest.mark.django_db
def test_record_edit_updates_content(client, django_user_model):
    user, domain = make_domain(django_user_model)
    zone = make_zone(domain)
    record = make_record(zone, content="1.2.3.4")
    client.force_login(user)
    url = reverse("dns:record_edit", kwargs={"domain_pk": domain.pk, "record_pk": record.pk})
    client.post(url, {
        "record_type": "A",
        "name": "@",
        "content": "5.6.7.8",
        "ttl": 3600,
        "priority": "",
        "proxied": "",
    })
    record.refresh_from_db()
    assert record.content == "5.6.7.8"


@pytest.mark.django_db
def test_record_edit_rejects_wrong_user(client, django_user_model):
    user, domain = make_domain(django_user_model)
    zone = make_zone(domain)
    record = make_record(zone)
    other = django_user_model.objects.create_user(email="other2@example.com", password="pass")
    client.force_login(other)
    url = reverse("dns:record_edit", kwargs={"domain_pk": domain.pk, "record_pk": record.pk})
    response = client.get(url)
    assert response.status_code == 404


# ──────────────────────────────────────────────
# record_delete
# ──────────────────────────────────────────────

@pytest.mark.django_db
def test_record_delete_soft_deletes(client, django_user_model):
    user, domain = make_domain(django_user_model)
    zone = make_zone(domain)
    record = make_record(zone)
    client.force_login(user)
    url = reverse("dns:record_delete", kwargs={"domain_pk": domain.pk, "record_pk": record.pk})
    response = client.post(url)
    assert response.status_code == 302
    record.refresh_from_db()
    assert record.is_active is False


@pytest.mark.django_db
def test_record_delete_requires_post(client, django_user_model):
    user, domain = make_domain(django_user_model)
    zone = make_zone(domain)
    record = make_record(zone)
    client.force_login(user)
    url = reverse("dns:record_delete", kwargs={"domain_pk": domain.pk, "record_pk": record.pk})
    response = client.get(url)
    assert response.status_code == 405


@pytest.mark.django_db
def test_record_delete_rejects_wrong_user(client, django_user_model):
    user, domain = make_domain(django_user_model)
    zone = make_zone(domain)
    record = make_record(zone)
    other = django_user_model.objects.create_user(email="other3@example.com", password="pass")
    client.force_login(other)
    url = reverse("dns:record_delete", kwargs={"domain_pk": domain.pk, "record_pk": record.pk})
    response = client.post(url)
    assert response.status_code == 404
    record.refresh_from_db()
    assert record.is_active is True


# ──────────────────────────────────────────────
# Cloudflare sync
# ──────────────────────────────────────────────

@pytest.mark.django_db
def test_cloudflare_sync_called_on_create(client, django_user_model, monkeypatch):
    """Adding a record in a Cloudflare zone triggers CF API call."""
    user, domain = make_domain(django_user_model)
    domain.cloudflare_zone_id = "fake-zone-id"
    domain.save()
    zone = make_zone(domain, provider="cloudflare")

    synced = {}

    def fake_create(self, zone_id, record_type, name, content, ttl, proxied):
        synced["called"] = True
        synced["zone_id"] = zone_id
        return {"result": {"id": "cf-record-id"}}

    from apps.cloudflare_integration import services as cf_services
    monkeypatch.setattr(cf_services.CloudflareService, "create_dns_record", fake_create)

    client.force_login(user)
    url = reverse("dns:record_add", kwargs={"domain_pk": domain.pk})
    client.post(url, {
        "record_type": "A",
        "name": "test",
        "content": "1.2.3.4",
        "ttl": 3600,
        "priority": "",
        "proxied": "",
    })

    assert synced.get("called") is True
    assert synced["zone_id"] == "fake-zone-id"
    record = DNSRecord.objects.get(name="test")
    assert record.external_id == "cf-record-id"


@pytest.mark.django_db
def test_non_cloudflare_zone_skips_sync(client, django_user_model, monkeypatch):
    """Non-CF zones do not trigger Cloudflare API calls."""
    user, domain = make_domain(django_user_model)
    zone = make_zone(domain, provider="registrar")

    called = {"value": False}

    def fake_create(*args, **kwargs):
        called["value"] = True

    from apps.cloudflare_integration import services as cf_services
    monkeypatch.setattr(cf_services.CloudflareService, "create_dns_record", fake_create)

    client.force_login(user)
    url = reverse("dns:record_add", kwargs={"domain_pk": domain.pk})
    client.post(url, {
        "record_type": "A",
        "name": "test2",
        "content": "5.6.7.8",
        "ttl": 3600,
        "priority": "",
        "proxied": "",
    })

    assert called["value"] is False
