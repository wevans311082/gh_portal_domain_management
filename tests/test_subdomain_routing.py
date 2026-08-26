import pytest
from django.test import RequestFactory, override_settings
from django.urls import resolve, reverse

from apps.core.middleware import SubdomainURLRoutingMiddleware


def test_subdomain_middleware_selects_portal_urlconf():
    request = RequestFactory().get("/", HTTP_HOST="portal.cyberask.co.uk")

    SubdomainURLRoutingMiddleware(lambda req: None)(request)

    assert request.urlconf == "cyberask_domains.urls_portal"


def test_subdomain_middleware_does_not_override_domains_host():
    request = RequestFactory().get("/", HTTP_HOST="domains.cyberask.co.uk")

    SubdomainURLRoutingMiddleware(lambda req: None)(request)

    assert getattr(request, "urlconf", None) is None


def test_subdomain_roots_resolve_to_service_entrypoints():
    portal_match = resolve("/", urlconf="cyberask_domains.urls_portal")
    domains_match = resolve("/", urlconf="cyberask_domains.urls_domains")
    billing_match = resolve("/", urlconf="cyberask_domains.urls_billing")

    assert portal_match.url_name == "dashboard"
    assert portal_match.namespace == "portal"
    assert domains_match.url_name == "search"
    assert domains_match.namespace == "domains"
    assert billing_match.url_name == "list"
    assert billing_match.namespace == "invoices"


@override_settings(ROOT_URLCONF="cyberask_domains.urls_domains")
def test_domains_urlconf_keeps_cross_service_links_reversible():
    assert reverse("domains:search") == "/"
    assert reverse("portal:cart") == "/portal/cart/"
    assert reverse("invoices:list") == "/billing/invoices/"
    assert reverse("payments:saved_cards") == "/billing/payments/cards/"
    assert reverse("core:pricing") == "/pricing/"
    assert reverse("core:contact") == "/contact/"
    assert reverse("core:health_check") == "/health/"
    assert reverse("provisioning:service_list") == "/hosting/"


@override_settings(ROOT_URLCONF="cyberask_domains.urls_billing")
def test_billing_urlconf_keeps_billing_links_at_root():
    assert reverse("invoices:list") == "/"
    assert reverse("payments:saved_cards") == "/payments/cards/"
    assert reverse("domains:search") == "/domains/"
    assert reverse("portal:dashboard") == "/portal/"


@override_settings(ROOT_URLCONF="cyberask_domains.urls_portal")
def test_portal_urlconf_keeps_portal_links_at_root():
    assert reverse("portal:dashboard") == "/"
    assert reverse("domains:search") == "/domains/"
    assert reverse("invoices:list") == "/billing/invoices/"
    assert reverse("payments:saved_cards") == "/billing/payments/cards/"
    assert reverse("core:pricing") == "/pricing/"
    assert reverse("core:health_check") == "/health/"


@pytest.mark.django_db
def test_products_list_redirects_to_shop(client):
    response = client.get(reverse("products:list"))
    assert response.status_code == 302
    assert response.url == reverse("portal:shop")
