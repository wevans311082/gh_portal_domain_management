"""Public failure contracts: browser recovery, JSON clients and safe markup."""
import pytest
from django.test import RequestFactory
from django.urls import resolve, reverse

from apps.core.abuse import PublicEndpointBoundaryMiddleware


def error_request(name="core:contact", **headers):
    request = RequestFactory().get(reverse(name), **headers)
    request.resolver_match = resolve(request.path)
    return request


@pytest.mark.parametrize("status", [400, 413, 429, 503])
@pytest.mark.django_db
def test_browser_errors_render_without_context_processor_database_work(status, django_assert_num_queries):
    request = error_request(HTTP_ACCEPT="text/html")
    with django_assert_num_queries(0):
        response = PublicEndpointBoundaryMiddleware.error(request, "Request failed <script>unsafe</script>", status, retry_after=60 if status == 429 else None)
    html = response.content.decode()
    assert response.status_code == status
    assert response["Content-Type"].startswith("text/html")
    assert "&lt;script&gt;" in html and "<script>unsafe" not in html
    assert 'aria-labelledby="request-title"' in html
    assert 'href="/contact/"' in html
    assert "Back button" in html
    assert response["Cache-Control"] == "no-store"
    assert "Accept" in response["Vary"] and "HX-Request" in response["Vary"]
    if status == 429:
        assert "60 seconds" in html


def test_htmx_failures_keep_machine_contract_and_explicit_marker():
    response = PublicEndpointBoundaryMiddleware.error(error_request(HTTP_ACCEPT="text/html", HTTP_HX_REQUEST="true"), "Please wait", 429, retry_after=75)
    import json
    assert json.loads(response.content) == {"error": "Please wait", "retry_after": 75}
    assert response["X-Public-Request-Error"] == "1"


def test_quote_json_contract_is_preserved_even_with_browser_accept():
    response = PublicEndpointBoundaryMiddleware.error(error_request("billing_public:quote_submit", HTTP_ACCEPT="text/html"), "Invalid details", 400)
    assert response["Content-Type"].startswith("application/json")


@pytest.mark.django_db
def test_contact_invalid_fields_have_error_associations_and_limits(client):
    response = client.post(reverse("core:contact"), {"name": "", "email": "invalid", "message": ""})
    html = response.content.decode()
    assert response.status_code == 200
    from html.parser import HTMLParser
    fields = {}
    class Fields(HTMLParser):
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag in {"input", "textarea"}:
                fields[attrs.get("id")] = attrs
    Fields().feed(html)
    for field in ("name", "email", "message"):
        assert f'id="id_{field}_error"' in html
        assert f'id_{field}_error' in fields[f'id_{field}']["aria-describedby"].split()
    assert 'maxlength="10000"' in html
    assert "10,000 characters" in html


@pytest.mark.django_db
def test_browser_throttle_contains_retry_guidance_and_header(client):
    for _ in range(5):
        client.post(reverse("core:contact"), {})
    response = client.post(reverse("core:contact"), {}, HTTP_ACCEPT="text/html")
    assert response.status_code == 429
    assert f'{response["Retry-After"]} seconds'.encode() in response.content
