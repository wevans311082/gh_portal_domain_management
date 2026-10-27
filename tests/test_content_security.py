"""SEC-07: inert JSON/SVG, raster-only persisted logos and CSP intersection."""
import io
import json
import re
import xml.etree.ElementTree as ET

import pytest
from django import forms
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.urls import reverse
from PIL import Image, PngImagePlugin

from apps.billing.models import BillingDocumentBranding
from apps.billing.uploads import sanitize_logo, MAX_LOGO_BYTES
from apps.core.middleware import ContentSecurityPolicyMiddleware
from apps.products.models import Package
from apps.website_templates.models import WebsiteTemplate
from tests.security_helpers import login_owner

pytestmark = pytest.mark.django_db


def raster(fmt="PNG", size=(12, 10)):
    buffer = io.BytesIO()
    image = Image.new("RGB", size, color="red")
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "<script>metadata</script>")
    image.save(buffer, format=fmt, **({"pnginfo": info} if fmt == "PNG" else {}))
    return buffer.getvalue()


def test_catalogue_script_termination_is_inert_and_round_trips(client):
    payload = '</script><script id="injected">alert(1)</script>&\u2028'
    Package.objects.create(name=payload, slug="payload-plan", quote_blurb=payload,
                           price_monthly=10, price_annually=100, is_active=True, is_quotable=True)
    response = client.get(reverse("billing_public:quote_builder"))
    html = response.content.decode()
    match = re.search(r'<script id="quote-catalogue" type="application/json">(.*?)</script>', html, re.S)
    assert match
    assert "<script" not in match[1] and "</script" not in match[1]
    assert "\\u003C" in match[1]
    data = json.loads(match[1])
    assert data[0]["items"][0]["name"] == payload
    assert data[0]["items"][0]["blurb"] == payload
    assert '<script id="injected">' not in html


def test_thumbnail_text_cannot_create_svg_elements(client, monkeypatch, tmp_path):
    monkeypatch.setattr("apps.website_templates.views.EXTRACTED_ROOT", tmp_path)
    name = '</text><script>alert(1)</script><text>&"\x01'
    template = WebsiteTemplate.objects.create(name=name, slug="svg-payload")
    response = client.get(reverse("website_templates:thumbnail", args=[template.slug]))
    root = ET.fromstring(response.content)
    ns = "{http://www.w3.org/2000/svg}"
    assert [element.tag for element in root] == [ns + "rect", ns + "text", ns + "text"]
    assert root.findall(ns + "text")[-1].text == name.replace("\x01", "")
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in response["Content-Security-Policy"]


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_logo_reencoding_discards_metadata_trailing_content_and_filename(fmt):
    upload = SimpleUploadedFile("../../active.html", raster(fmt) + b"<script>trailing</script>", content_type="text/html")
    clean = sanitize_logo(upload)
    assert re.fullmatch(r"[a-f0-9]{32}\.png", clean.name)
    assert b"<script>" not in clean.read()
    clean.seek(0)
    with Image.open(clean) as image:
        assert image.format == "PNG"
        assert image.size == (12, 10)
        assert not image.info
        image.load()


@pytest.mark.parametrize("data", [
    b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
    b"<html><script>alert(1)</script></html>", b"not an image", raster()[:35], raster("GIF"),
])
def test_active_invalid_or_unsupported_files_are_rejected_despite_image_name(data):
    with pytest.raises(ValidationError):
        sanitize_logo(SimpleUploadedFile("logo.png", data, content_type="image/png"))


def test_logo_actual_read_is_bounded_even_if_reported_size_is_false():
    upload = SimpleUploadedFile("logo.png", b"x" * (MAX_LOGO_BYTES + 1))
    upload.size = 1
    with pytest.raises(ValidationError, match="5 MB"):
        sanitize_logo(upload)


@pytest.mark.parametrize("size", [(4097, 1), (3000, 3000)])
def test_logo_dimensions_are_bounded_before_decoding_pixels(size):
    with pytest.raises(ValidationError, match="dimensions"):
        sanitize_logo(SimpleUploadedFile("large.png", raster(size=size)))


def test_animated_logo_is_rejected():
    buffer = io.BytesIO()
    first = Image.new("RGB", (5, 5), "red")
    first.save(buffer, format="PNG", save_all=True, append_images=[Image.new("RGB", (5, 5), "blue")])
    with pytest.raises(ValidationError, match="static"):
        sanitize_logo(SimpleUploadedFile("animation.png", buffer.getvalue()))


def test_decoder_bomb_protection_is_fail_closed(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(ValidationError, match="safe image"):
        sanitize_logo(SimpleUploadedFile("bomb.png", raster()))


def test_encoded_output_size_is_bounded(monkeypatch):
    monkeypatch.setattr("apps.billing.uploads.MAX_OUTPUT_BYTES", 8)
    with pytest.raises(ValidationError, match="decoded logo is too large"):
        sanitize_logo(SimpleUploadedFile("large-output.png", raster()))


def test_native_model_form_validates_logo():
    class BrandingForm(forms.ModelForm):
        class Meta:
            model = BillingDocumentBranding
            fields = ["logo"]
    form = BrandingForm(files={"logo": SimpleUploadedFile("bad.html", b"<html>active</html>")})
    assert not form.is_valid()
    assert "logo" in form.errors


def test_direct_storage_save_cannot_bypass_logo_sanitizer(tmp_path):
    with override_settings(MEDIA_ROOT=tmp_path):
        branding = BillingDocumentBranding.get_solo()
        with pytest.raises(ValidationError):
            branding.logo.save("active.html", SimpleUploadedFile("active.html", b"<html>active</html>"))
        assert not list(tmp_path.rglob("*.*"))
        branding.logo.save("active.html", SimpleUploadedFile("active.html", raster()))
        branding.refresh_from_db()
        assert branding.logo.name.endswith(".png")
        assert "active.html" not in branding.logo.name
        with branding.logo.open() as stored:
            with Image.open(stored) as image:
                assert image.format == "PNG"


def test_invalid_logo_post_does_not_partially_save_branding(client, django_user_model, tmp_path):
    login_owner(client, django_user_model.objects.create_user(email="branding-owner@example.test"))
    branding = BillingDocumentBranding.get_solo()
    original = branding.company_name
    with override_settings(MEDIA_ROOT=tmp_path):
        response = client.post(reverse("admin_tools:billing_branding"), {
            "company_name": "Must not persist", "logo": SimpleUploadedFile("logo.png", b"<svg>active</svg>"),
        })
    assert response.status_code == 200
    assert b"The branding was not saved" in response.content
    assert b'aria-invalid="true"' in response.content
    branding.refresh_from_db()
    assert branding.company_name == original
    assert not branding.logo
    assert not list(tmp_path.rglob("*.*"))


def test_valid_logo_post_is_saved_as_generated_png(client, django_user_model, tmp_path):
    login_owner(client, django_user_model.objects.create_user(email="valid-branding@example.test"))
    with override_settings(MEDIA_ROOT=tmp_path):
        response = client.post(reverse("admin_tools:billing_branding"), {
            "logo": SimpleUploadedFile("not-html.html", raster(), content_type="text/html"),
        })
        assert response.status_code == 302
        branding = BillingDocumentBranding.get_solo()
        assert re.fullmatch(r"billing/branding/[a-f0-9]{32}\.png", branding.logo.name)
        assert b"metadata" not in branding.logo.read()


def test_conflicting_upload_and_removal_preserves_existing_logo(client, django_user_model, tmp_path):
    login_owner(client, django_user_model.objects.create_user(email="conflicting-branding@example.test"))
    with override_settings(MEDIA_ROOT=tmp_path):
        branding = BillingDocumentBranding.get_solo()
        branding.logo.save("old.png", SimpleUploadedFile("old.png", raster()))
        original = branding.logo.name
        response = client.post(reverse("admin_tools:billing_branding"), {
            "clear_logo": "1", "logo": SimpleUploadedFile("new.png", raster()),
        })
        assert response.status_code == 200
        branding.refresh_from_db()
        assert branding.logo.name == original
        assert branding.logo.storage.exists(original)


@override_settings(CSP_DEFAULT_SRC=["'self'"], CSP_SCRIPT_SRC=["'self'"])
def test_response_specific_csp_survives_as_an_independent_policy():
    def view(request):
        response = HttpResponse("preview")
        response["Content-Security-Policy"] = "sandbox allow-scripts; script-src 'none'"
        return response
    response = ContentSecurityPolicyMiddleware(view)(RequestFactory().get("/"))
    policies = response["Content-Security-Policy"].split(", ")
    assert policies[0] == "sandbox allow-scripts; script-src 'none'"
    assert "default-src 'self'" in policies[1]
    assert "script-src 'self'" in policies[1]
