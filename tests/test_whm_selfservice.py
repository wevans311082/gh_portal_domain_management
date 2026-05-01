"""
Tests for the WHM/cPanel self-service portal.

All WHM API calls are mocked — no real server required.
"""
import pytest
from unittest.mock import MagicMock, patch
from django.urls import reverse

from apps.provisioning.forms import DatabaseForm, EmailAccountForm
from apps.provisioning.models import ProvisioningJob
from apps.services.models import Service
from apps.products.models import Package


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def make_user(django_user_model, email="whm@example.com"):
    return django_user_model.objects.create_user(email=email, password="testpass123")


def make_package(slug=None):
    import uuid
    slug = slug or f"starter-{uuid.uuid4().hex[:8]}"
    return Package.objects.create(
        name="Starter",
        slug=slug,
        price_monthly="9.99",
        price_annually="99.99",
        whm_package_name="starter_pkg",
    )


def make_service(user, cpanel_username="testuser", status=Service.STATUS_ACTIVE):
    return Service.objects.create(
        user=user,
        package=make_package(),
        domain_name="example.com",
        cpanel_username=cpanel_username,
        status=status,
    )


MOCK_EMAILS = [
    {"user": "info", "domain": "example.com", "diskusedmb": "5", "diskquota": "500"},
    {"user": "sales", "domain": "example.com", "diskusedmb": "2", "diskquota": "500"},
]

MOCK_DATABASES = [
    {"database": "testuser_myapp"},
    {"database": "testuser_wordpress"},
]

MOCK_QUOTA = {"megabytes_used": 250, "megabytes_limit": 2048}

VALID_EMAIL_PAYLOAD = {
    "email_user": "newbox",
    "password": "SecureP@ss1",
    "password_confirm": "SecureP@ss1",
    "quota_mb": 500,
}

VALID_DB_PAYLOAD = {
    "db_name": "my_database",
}


# ─────────────────────────────────────────────
# Forms unit tests
# ─────────────────────────────────────────────

def test_email_form_valid():
    form = EmailAccountForm(data=VALID_EMAIL_PAYLOAD)
    assert form.is_valid(), form.errors


def test_email_form_mismatched_passwords():
    data = dict(VALID_EMAIL_PAYLOAD, password_confirm="wrong")
    form = EmailAccountForm(data=data)
    assert not form.is_valid()
    assert "Passwords do not match" in str(form.errors)


def test_email_form_invalid_chars_in_user():
    data = dict(VALID_EMAIL_PAYLOAD, email_user="bad name!")
    form = EmailAccountForm(data=data)
    assert not form.is_valid()


def test_db_form_valid():
    form = DatabaseForm(data=VALID_DB_PAYLOAD)
    assert form.is_valid(), form.errors


def test_db_form_invalid_chars():
    form = DatabaseForm(data={"db_name": "bad-name!"})
    assert not form.is_valid()


def test_db_form_empty():
    form = DatabaseForm(data={"db_name": ""})
    assert not form.is_valid()


# ─────────────────────────────────────────────
# service_list
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_service_list_requires_login(client):
    assert client.get(reverse("provisioning:service_list")).status_code == 302


@pytest.mark.django_db
def test_service_list_shows_own_services(client, django_user_model):
    user = make_user(django_user_model)
    make_service(user)
    other = django_user_model.objects.create_user(email="other@example.com", password="pass")
    make_service(other, cpanel_username="otheruser")
    client.force_login(user)
    response = client.get(reverse("provisioning:service_list"))
    assert response.status_code == 200
    assert b"example.com" in response.content
    # the page should only list the current user's services - check service count
    assert response.context["services"].count() == 1


# ─────────────────────────────────────────────
# service_detail
# ─────────────────────────────────────────────

@pytest.mark.django_db
def test_service_detail_requires_login(client, django_user_model):
    user = make_user(django_user_model)
    service = make_service(user)
    assert client.get(reverse("provisioning:service_detail", args=[service.id])).status_code == 302


@pytest.mark.django_db
@patch("apps.provisioning.views.WHMClient")
def test_service_detail_renders_email_and_db(mock_whm_cls, client, django_user_model):
    mock_client = MagicMock()
    mock_client.list_email_accounts.return_value = MOCK_EMAILS
    mock_client.list_databases.return_value = MOCK_DATABASES
    mock_client.get_quota.return_value = MOCK_QUOTA
    mock_whm_cls.return_value = mock_client

    user = make_user(django_user_model)
    service = make_service(user)
    client.force_login(user)
    response = client.get(reverse("provisioning:service_detail", args=[service.id]))

    assert response.status_code == 200
    assert b"info" in response.content          # email account
    assert b"testuser_myapp" in response.content  # database


@pytest.mark.django_db
@patch("apps.provisioning.views.WHMClient")
def test_service_detail_whm_error_shows_message(mock_whm_cls, client, django_user_model):
    from apps.provisioning.whm_client import WHMClientError
    mock_client = MagicMock()
    mock_client.list_email_accounts.side_effect = WHMClientError("connection refused")
    mock_whm_cls.return_value = mock_client

    user = make_user(django_user_model)
    service = make_service(user)
    client.force_login(user)
    response = client.get(reverse("provisioning:service_detail", args=[service.id]))

    assert response.status_code == 200
    assert b"Could not fetch live cPanel data" in response.content


@pytest.mark.django_db
def test_service_detail_rejects_other_user(client, django_user_model):
    owner = make_user(django_user_model, email="owner@example.com")
    other = django_user_model.objects.create_user(email="other4@example.com", password="pass")
    service = make_service(owner)
    client.force_login(other)
    assert client.get(reverse("provisioning:service_detail", args=[service.id])).status_code == 404


# ─────────────────────────────────────────────
# email_create
# ─────────────────────────────────────────────

@pytest.mark.django_db
@patch("apps.provisioning.views.create_email_account_task")
def test_email_create_queues_task(mock_task, client, django_user_model):
    user = make_user(django_user_model)
    service = make_service(user)
    client.force_login(user)
    response = client.post(
        reverse("provisioning:email_create", args=[service.id]),
        VALID_EMAIL_PAYLOAD,
    )
    assert response.status_code == 302
    mock_task.delay.assert_called_once()
    call_kwargs = mock_task.delay.call_args.kwargs
    assert call_kwargs["email_user"] == "newbox"
    assert call_kwargs["domain"] == "example.com"


@pytest.mark.django_db
def test_email_create_invalid_form_rerenders(client, django_user_model):
    user = make_user(django_user_model)
    service = make_service(user)
    client.force_login(user)
    bad = dict(VALID_EMAIL_PAYLOAD, password_confirm="wrong")
    response = client.post(reverse("provisioning:email_create", args=[service.id]), bad)
    assert response.status_code == 200
    assert b"Passwords do not match" in response.content


@pytest.mark.django_db
def test_email_create_no_cpanel_username_redirects(client, django_user_model):
    user = make_user(django_user_model)
    service = make_service(user, cpanel_username="")
    client.force_login(user)
    response = client.post(reverse("provisioning:email_create", args=[service.id]), VALID_EMAIL_PAYLOAD)
    assert response.status_code == 302


# ─────────────────────────────────────────────
# email_delete
# ─────────────────────────────────────────────

@pytest.mark.django_db
@patch("apps.provisioning.views.delete_email_account_task")
def test_email_delete_queues_task(mock_task, client, django_user_model):
    user = make_user(django_user_model)
    service = make_service(user)
    client.force_login(user)
    response = client.post(
        reverse("provisioning:email_delete", args=[service.id]),
        {"email_user": "info", "domain": "example.com"},
    )
    assert response.status_code == 302
    mock_task.delay.assert_called_once_with(
        service_id=service.id,
        email_user="info",
        domain="example.com",
    )


@pytest.mark.django_db
def test_email_delete_requires_post(client, django_user_model):
    user = make_user(django_user_model)
    service = make_service(user)
    client.force_login(user)
    assert client.get(reverse("provisioning:email_delete", args=[service.id])).status_code == 405


@pytest.mark.django_db
def test_email_delete_missing_params_redirects(client, django_user_model):
    user = make_user(django_user_model)
    service = make_service(user)
    client.force_login(user)
    response = client.post(reverse("provisioning:email_delete", args=[service.id]), {})
    assert response.status_code == 302


# ─────────────────────────────────────────────
# database_create
# ─────────────────────────────────────────────

@pytest.mark.django_db
@patch("apps.provisioning.views.create_database_task")
def test_database_create_queues_task(mock_task, client, django_user_model):
    user = make_user(django_user_model)
    service = make_service(user)
    client.force_login(user)
    response = client.post(
        reverse("provisioning:database_create", args=[service.id]),
        VALID_DB_PAYLOAD,
    )
    assert response.status_code == 302
    mock_task.delay.assert_called_once_with(
        service_id=service.id,
        db_name="my_database",
    )


@pytest.mark.django_db
def test_database_create_invalid_name_rerenders(client, django_user_model):
    user = make_user(django_user_model)
    service = make_service(user)
    client.force_login(user)
    response = client.post(
        reverse("provisioning:database_create", args=[service.id]),
        {"db_name": "bad-name!"},
    )
    assert response.status_code == 200


@pytest.mark.django_db
def test_database_create_rejects_other_user(client, django_user_model):
    owner = make_user(django_user_model, email="owner2@example.com")
    other = django_user_model.objects.create_user(email="other5@example.com", password="pass")
    service = make_service(owner)
    client.force_login(other)
    assert client.post(
        reverse("provisioning:database_create", args=[service.id]),
        VALID_DB_PAYLOAD,
    ).status_code == 404


# ─────────────────────────────────────────────
# Celery tasks unit tests
# ─────────────────────────────────────────────

@pytest.mark.django_db
@patch("apps.provisioning.tasks.WHMClient")
def test_create_email_account_task_calls_client(mock_whm_cls, django_user_model):
    from apps.provisioning.tasks import create_email_account_task

    mock_client = MagicMock()
    mock_whm_cls.return_value = mock_client

    user = make_user(django_user_model)
    service = make_service(user)

    create_email_account_task(service.id, "info", "example.com", "P@ssword1", 500)
    mock_client.create_email_account.assert_called_once_with(
        cpanel_username="testuser",
        email_user="info",
        domain="example.com",
        password="P@ssword1",
        quota_mb=500,
    )


@pytest.mark.django_db
@patch("apps.provisioning.tasks.WHMClient")
def test_create_email_account_task_skips_no_cpanel_username(mock_whm_cls, django_user_model):
    from apps.provisioning.tasks import create_email_account_task

    user = make_user(django_user_model)
    service = make_service(user, cpanel_username="")

    create_email_account_task(service.id, "info", "example.com", "P@ssword1", 500)
    mock_whm_cls.assert_not_called()


@pytest.mark.django_db
@patch("apps.provisioning.tasks.WHMClient")
def test_create_database_task_uses_prefixed_name(mock_whm_cls, django_user_model):
    from apps.provisioning.tasks import create_database_task

    mock_client = MagicMock()
    mock_whm_cls.return_value = mock_client

    user = make_user(django_user_model)
    service = make_service(user)

    create_database_task(service.id, "myapp")
    mock_client.create_database.assert_called_once_with(
        cpanel_username="testuser",
        db_name="testuser_myapp",
    )


@pytest.mark.django_db
def test_create_email_account_task_missing_service():
    from apps.provisioning.tasks import create_email_account_task
    # Should not raise — just log and return
    create_email_account_task(999999, "info", "example.com", "pass", 500)


@pytest.mark.django_db
def test_create_database_task_missing_service():
    from apps.provisioning.tasks import create_database_task
    create_database_task(999999, "mydb")
