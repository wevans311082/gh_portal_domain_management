"""Render isolated authentication/staff-access fixtures and check accessibility.

Usage: .venv/Scripts/python.exe scripts/verify_authentication_ui.py --axe PATH_TO_AXE_MIN_JS
Requires Playwright and an installed Chrome. Uses only an in-memory test database.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axe", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path(tempfile.gettempdir()) / "portal-authentication-ui")
    parser.add_argument("--pages", nargs="+", help="Limit a focused rerun to named fixture pages.")
    args = parser.parse_args()
    if not args.axe.is_file():
        parser.error("--axe must point to a local axe-core browser bundle.")

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    os.environ["DJANGO_SETTINGS_MODULE"] = "cyberask_domains.settings.test"
    os.environ["DJANGO_SECRET_KEY"] = "isolated-authentication-ui-test-key"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    import django
    django.setup()
    from django.core.management import call_command
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group
    from django.test import Client, override_settings
    from django.urls import reverse
    from playwright.sync_api import sync_playwright
    import pyotp

    call_command("migrate", verbosity=0)
    fixtures = {}
    with override_settings(ALLOWED_HOSTS=["testserver"]):
        client = Client()
        fixtures["/login"] = client.get(reverse("account_login")).content
        for name, route in (("public-mail-reset", "account_reset_password_done"), ("public-mail-verification", "account_email_verification_sent")):
            response = client.get(reverse(route))
            assert response.status_code == 200, (route, response.status_code)
            fixtures[f"/{name}"] = response.content
        fixtures["/public-contact"] = client.get(reverse("core:contact")).content
        fixtures["/public-contact-invalid"] = client.post(reverse("core:contact"), {"email": "invalid"}).content
        from apps.core.abuse import PublicEndpointBoundaryMiddleware
        from django.test import RequestFactory
        from django.urls import resolve
        for status in (413, 429, 503):
            request = RequestFactory().get(reverse("core:contact"), HTTP_ACCEPT="text/html")
            request.resolver_match = resolve(request.path)
            fixtures[f"/public-error-{status}"] = PublicEndpointBoundaryMiddleware.error(
                request, "Please wait before trying again." if status == 429 else "The request could not be completed.",
                status, retry_after=60 if status == 429 else None).content
        from apps.products.models import Package
        Package.objects.create(name="Fixture hosting", slug="ui-hosting", price_monthly="10", price_annually="100", setup_fee="2", is_quotable=True)
        fixtures["/public-quote"] = client.get(reverse("billing_public:quote_builder")).content
        from unittest.mock import patch
        with patch("apps.domains.views._sync_missing_tld_pricing"):
            fixtures["/public-domain-error"] = client.get(reverse("domains:search")).content

        user = get_user_model().objects.create_user(
            email="test@example.test", password="test-password", mfa_enabled=True,
            mfa_secret=pyotp.random_base32(),
        )
        client.post(reverse("account_login"), {"email": user.email, "password": "test-password"})
        fixtures["/mfa"] = client.get(reverse("accounts_custom:mfa_verify")).content
        fixtures["/mfa-invalid"] = client.post(reverse("accounts_custom:mfa_verify"), {"code": "incorrect"}).content
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=["is_staff", "is_superuser"])
        client.post(reverse("account_login"), {"email": user.email, "password": "test-password"})
        client.post(reverse("accounts_custom:mfa_verify"), {"code": pyotp.TOTP(user.mfa_secret).now()})
        fixtures["/reauthenticate"] = client.get(reverse("accounts_custom:reauthenticate")).content
        fixtures["/registrant-contact"] = client.get(reverse("domains:contact_create")).content
        fixtures["/registrant-contact-invalid"] = client.post(
            reverse("domains:contact_create"), {"registrant_type": "individual", "name": "Jane Smith"}
        ).content
        from apps.domains.models import TLDPricing
        for extension in ("co.uk", "ca"):
            TLDPricing.objects.get_or_create(tld=extension, defaults={"registration_cost": "10", "is_active": True})
        fixtures["/registrant-domain-uk"] = client.get(reverse("domains:register"), {"domain": "fixture-domain.co.uk"}).content
        fixtures["/registrant-domain-review"] = client.get(reverse("domains:register"), {"domain": "fixture-domain.ca"}).content
        from apps.portal.cart_service import add_domain_registration_item, get_active_cart, create_quote_from_cart
        TLDPricing.objects.get_or_create(tld="com", defaults={"registration_cost": "10", "is_active": True})
        add_domain_registration_item(user=user, domain_name="cart-" + "x" * 58 + ".com")
        fixtures["/portal-cart"] = client.get(reverse("portal:cart")).content
        cart_quote = create_quote_from_cart(get_active_cart(user))
        fixtures["/public-cart-review"] = client.post(reverse("billing_public:quote_public_accept", args=[cart_quote.public_token])).content
        fixtures["/portal-cart-empty"] = client.get(reverse("portal:cart")).content
        from apps.domains.models import ContactVerification, Domain, DomainOrder
        from django.utils import timezone
        from datetime import timedelta
        verification_contact = user.domain_contacts.first()
        verification_contact.registrar_contact_id = "12345"
        verification_contact.save(update_fields=["registrar_contact_id"])
        verification_domain = Domain.objects.create(user=user, name="verification-ui.com", tld="com", status="active", registrar_id="98765")
        verification_order = DomainOrder.objects.create(
            user=user, domain=verification_domain, domain_name=verification_domain.name, tld="com",
            registrar_order_id="98765", registration_contact=verification_contact,
            admin_contact=verification_contact, tech_contact=verification_contact, billing_contact=verification_contact,
        )
        with patch("apps.provisioning.whm_client.WHMClient") as whm:
            whm.return_value.get_nameservers.return_value = []
            for status in ("verified", "pending", "suspended", "unknown"):
                ContactVerification.objects.create(
                    contact=verification_contact, domain_order=verification_order, domain_name=verification_domain.name,
                    check_kind="registrar_email", source="resellerclub", reference="98765", tld="com", status=status,
                    subject_digest=ContactVerification.digest_for(verification_contact), checked_at=timezone.now(),
                    expires_at=timezone.now() + timedelta(minutes=15),
                    action_due_at=timezone.now() + timedelta(days=14) if status in {"pending", "suspended"} else None,
                )
                fixtures[f"/registrant-verification-{status}"] = client.get(reverse("domains:detail", kwargs={"pk": verification_domain.pk})).content
            from apps.domains.verification_actions import request_verification_action
            import uuid
            fixture_refresh = request_verification_action(verification_domain, user, "refresh", uuid.uuid4())
            fixtures["/registrant-verification-queued"] = client.get(reverse("domains:detail", kwargs={"pk": verification_domain.pk})).content
            fixtures["/registrant-verification-error"] = client.post(
                reverse("domains:verification_action", kwargs={"pk": verification_domain.pk}),
                {"kind": "refresh", "key": str(uuid.uuid4())},
            ).content
            from apps.domains.models import VerificationAction
            VerificationAction.objects.filter(pk=fixture_refresh.pk).update(
                status="succeeded", created_at=timezone.now() - timedelta(minutes=16),
            )
            request_verification_action(verification_domain, None, "refresh", uuid.uuid4(), automatic=True)
            fixtures["/registrant-verification-automatic"] = client.get(reverse("domains:detail", kwargs={"pk": verification_domain.pk})).content
            VerificationAction.objects.create(
                domain_order=verification_order, actor=user, key=uuid.uuid4(), kind="resend", status="unknown",
                subject_digest=ContactVerification.digest_for(verification_contact), registrar_order_id="98765",
            )
            pending_evidence = ContactVerification.objects.filter(domain_order=verification_order, status="pending").first()
            pending_evidence.pk = None
            pending_evidence.checked_at = timezone.now()
            pending_evidence.save()
            fixtures["/registrant-verification-review"] = client.get(reverse("domains:detail", kwargs={"pk": verification_domain.pk})).content
        with patch("apps.admin_tools.manual_order_views._load_whm_hosting_packages", return_value=([], "fixture")):
            fixtures["/admin-manual-domain"] = client.get(reverse("admin_tools:manual_domain_register")).content
            fixtures["/admin-manual-domain-invalid"] = client.post(
                reverse("admin_tools:manual_domain_register"), {
                    "domain_name": "manual-fixture.com", "user_id": user.pk,
                    "contact_registrant_type": "individual", "contact_phone": "12",
                    "contact_country": "ZZ",
                }
            ).content
        for index in range(32):
            get_user_model().objects.create_user(email=f"customer-{index:02d}@example.test", password="fixture-password")
        fixtures["/admin-customers"] = client.get(reverse("admin_tools:users")).content
        fixtures["/admin-customers-empty"] = client.get(reverse("admin_tools:users"), {"q": "no-matching-customer-zz"}).content
        fixtures["/admin-customers-loading"] = fixtures["/admin-customers"]
        fixtures["/admin-user-invalid"] = client.post(reverse("admin_tools:user_create"), {}).content
        fixtures["/admin-invoices-empty"] = client.get(reverse("admin_tools:invoices")).content
        from apps.billing.models import Invoice
        for index, status in enumerate(("paid", "overdue", "unpaid")):
            Invoice.objects.create(user=user, number=f"UI-{index}", status=status, total="120", vat_rate="20")
        fixtures["/admin-invoices"] = client.get(reverse("admin_tools:invoices")).content
        from apps.domains.models import Domain
        from apps.services.models import Service
        from apps.support.models import SupportTicket
        from apps.billing.models import Quote
        search_customer = get_user_model().objects.create_user(email="globalsearch@example.test", first_name="Global", last_name="Search", password="fixture-password")
        search_domain = Domain.objects.create(user=search_customer, name="globalsearch.example", tld="example", epp_code="NOT-FOR-SEARCH")
        Service.objects.create(user=search_customer, package=Package.objects.get(slug="ui-hosting"), domain_name="globalsearch.host")
        SupportTicket.objects.create(user=search_customer, subject="Fixture customer request")
        Quote.objects.create(user=search_customer, number="SEARCH-Q-001", total="120")
        for index in range(26):
            Invoice.objects.create(user=search_customer, number=f"SEARCH-I-{index:02d}", total="120")
        search_url = reverse("admin_tools:search")
        fixtures["/admin-search-start"] = client.get(search_url).content
        fixtures["/admin-search-suggestions-error"] = fixtures["/admin-search-start"]
        fixtures["/admin-search"] = client.get(search_url, {"q": "global"}).content
        fixtures["/admin-search-empty"] = client.get(search_url, {"q": "no-record-for-this-key-zz"}).content
        fixtures["/admin-search-invalid"] = client.get(search_url, {"q": "x"}).content
        fixtures["/admin-search-pagination"] = client.get(search_url, {"q": "SEARCH-I", "group": "invoices", "page": "2"}).content
        from apps.admin_tools.models import CustomerNote
        from apps.accounts.models import ClientProfile
        from apps.domains.models import DomainContact
        ClientProfile.objects.create(user=search_customer, is_business=False)
        search_contact = DomainContact.objects.create(
            user=search_customer, registrant_type="individual", label="Individual registrant", name="Global Search",
            email=search_customer.email, address_line1="1 High Street", city="London", state="London",
            postcode="SW1A 1AA", country="GB", phone_country_code="44", phone="07700900000",
        )
        search_customer.email = "customer-" + "x" * 110 + "@example.test"
        org_user = get_user_model().objects.create_user(email="publication-ui@example.test", password="fixture-password")
        org_contact = DomainContact.objects.create(user=org_user, registrant_type="organisation", label="Organization registrant",
            name="Jane Smith", company="Example Customer Ltd", email=org_user.email, address_line1="1 High Street",
            city="London", state="London", postcode="SW1A 1AA", country="GB", phone_country_code="44", phone="07700900000", is_default=True)
        org_client = Client()
        org_client.force_login(org_user)
        fixtures["/publication-registration"] = org_client.get(reverse("domains:register"), {"domain": "publication-ui.com"}).content
        fixtures["/publication-empty"] = org_client.get(reverse("domains:publication_decisions")).content
        fixtures["/publication-shop"] = org_client.get(reverse("portal:shop")).content
        publication_item = add_domain_registration_item(user=org_user, domain_name="publication-ui.com",
            domain_contact_id=org_contact.pk, publication_choice="publish")
        fixtures["/publication-cart"] = org_client.get(reverse("portal:cart")).content
        fixtures["/publication-active"] = org_client.get(reverse("domains:publication_decisions")).content
        org_contact.company = "Changed Organization Ltd"
        org_contact.save(update_fields=["company"])
        fixtures["/publication-stale-cart"] = org_client.post(reverse("portal:cart_checkout_invoice"), follow=True).content
        org_client.post(reverse("domains:publication_withdraw", args=[publication_item.publication_decision_id]))
        fixtures["/publication-withdrawn"] = org_client.get(reverse("domains:publication_decisions")).content
        from apps.domains.publication_consent import capture_decision
        submitted_decision = capture_decision(customer=org_user, contact=org_contact, domain_name="submitted-publication-ui.com", choice="publish")
        DomainOrder.objects.create(user=org_user, domain_name=submitted_decision.domain_name, tld="com", registrar_order_id="502",
            status="processing", registration_contact=org_contact, admin_contact=org_contact, tech_contact=org_contact,
            billing_contact=org_contact, publication_decision=submitted_decision)
        org_client.post(reverse("domains:publication_withdraw", args=[submitted_decision.pk]))
        fixtures["/publication-provider-pending"] = org_client.get(reverse("domains:publication_decisions")).content
        from apps.domains.models import PublicationRedaction
        from django.utils import timezone
        for redaction_state in ("review", "unknown", "awaiting", "confirmed"):
            PublicationRedaction.objects.filter(decision=submitted_decision).update(status=redaction_state,
                confirmed_at=timezone.now() if redaction_state == "confirmed" else None)
            fixtures[f"/publication-redaction-{redaction_state}"] = org_client.get(reverse("domains:publication_decisions")).content
        for extension in ("me.uk", "uk"):
            TLDPricing.objects.get_or_create(tld=extension, defaults={"registration_cost": "10", "renewal_cost": "10", "is_active": True})
        fixtures["/registry-me-registration"] = org_client.get(reverse("domains:register"), {"domain": "personal-policy.me.uk"}).content
        rejected_uk = org_client.post(reverse("domains:register"), {"domain_name": "organization-policy.me.uk",
            "contact": org_contact.pk, "registration_years": "1", "dns_provider": "cpanel", "publication_choice": "redact"})
        assert rejected_uk.status_code == 200 and b"personal namespace" in rejected_uk.content
        fixtures["/registry-me-invalid"] = rejected_uk.content
        uk_domain = Domain.objects.create(user=org_user, name="individual-policy.co.uk", tld="com", status="active", registrar_id="601", epp_code="PRIVATE-UK-EPP-CACHED")
        with patch("apps.provisioning.whm_client.WHMClient") as uk_whm:
            uk_whm.return_value.get_nameservers.return_value = []
            fixtures["/registry-uk-domain"] = org_client.get(reverse("domains:detail", args=[uk_domain.pk])).content
        denied_uk = org_client.post(reverse("domains:get_auth_code", args=[uk_domain.pk]))
        assert denied_uk.status_code == 409
        fixtures["/registry-uk-operation"] = denied_uk.content
        me_domain = Domain.objects.create(user=org_user, name="review-policy.me.uk", tld="me.uk", status="active", registrar_id="602")
        reviewed_me = org_client.get(reverse("domains:renew", args=[me_domain.pk]))
        assert reviewed_me.status_code == 409
        fixtures["/registry-me-renewal-review"] = reviewed_me.content
        with patch("apps.provisioning.whm_client.WHMClient") as me_whm:
            me_whm.return_value.get_nameservers.return_value = []
            fixtures["/registry-me-held-domain"] = org_client.get(reverse("domains:detail", args=[me_domain.pk])).content
        fixtures["/registry-me-domain-list"] = org_client.get(reverse("domains:my_domains")).content
        from apps.domains.models import DomainRegistrationAttempt, DomainContactChange
        from apps.domains.contact_bindings import provider_scope
        contact_client = Client()
        contact_client.force_login(search_customer)
        contact_domain = Domain.objects.create(user=search_customer, name="contact-change-ui.com", tld="com",
            status="active", registrar_id="7501")
        contact_order = DomainOrder.objects.create(user=search_customer, domain=contact_domain, domain_name=contact_domain.name,
            tld="com", status="completed", registrar_order_id="7501", registration_contact=search_contact,
            admin_contact=search_contact, tech_contact=search_contact, billing_contact=search_contact,
            registrar_contact_ids={"reg": "7201", "admin": "7201", "tech": "7201", "billing": "7201", "_scope": provider_scope()})
        DomainRegistrationAttempt.objects.create(order=contact_order, status="accepted", provider_scope=provider_scope(),
            customer_id="7999", remote_order_id="7501")
        contact_url = reverse("domains:contact_change", args=[contact_domain.pk])
        fixtures["/portal-contact-change-form"] = contact_client.get(contact_url).content
        fixtures["/portal-contact-change-invalid"] = contact_client.post(contact_url, {"key": str(uuid.uuid4())}).content
        change_post = {"key": str(uuid.uuid4()), "confirm": "on", "publication_choice": "redact",
            **{role: search_contact.pk for role in ("registration_contact", "admin_contact", "tech_contact", "billing_contact")}}
        assert contact_client.post(contact_url, change_post).status_code == 302
        fixtures["/portal-contact-change-queued"] = contact_client.get(contact_url).content
        contact_change = DomainContactChange.objects.get(domain=contact_domain)
        for state, irtp in (("awaiting", "APPROVED"), ("unknown", "PENDING"), ("confirmed", "SUCCESS")):
            DomainContactChange.objects.filter(pk=contact_change.pk).update(status=state, irtp_status=irtp)
            fixtures[f"/portal-contact-change-{state}"] = contact_client.get(contact_url).content
        contact_order.registration_attempt.delete()
        fixtures["/portal-contact-change-review"] = contact_client.get(contact_url).content
        search_customer.save(update_fields=["email"])
        CustomerNote.objects.create(customer=search_customer, author=user, body="Staff service context.\n" + "long-note-" * 45 + "\n<script>Escaped note text</script>", submission_key=uuid.uuid4())
        workspace_url = reverse("admin_tools:customer_workspace", args=[search_customer.pk])
        response = client.get(workspace_url)
        assert response.status_code == 200
        fixtures["/admin-customer-workspace"] = response.content
        fixtures["/admin-customer-workspace-loading"] = response.content
        fixtures["/admin-customer-workspace-invalid"] = client.post(reverse("admin_tools:customer_note_add", args=[search_customer.pk]), {"submission_key": str(uuid.uuid4()), "body": ""}).content
        empty_customer = get_user_model().objects.create_user(email="workspace-empty@example.test", password="fixture-password")
        fixtures["/admin-customer-workspace-empty"] = client.get(reverse("admin_tools:customer_workspace", args=[empty_customer.pk])).content
        redacted_note = CustomerNote.objects.create(customer=search_customer, author=user, body="", submission_key=uuid.uuid4(), redacted_at=timezone.now(), redacted_by=user)
        fixtures["/admin-customer-note-redacted"] = client.get(reverse("admin_tools:customer_note", args=[search_customer.pk, redacted_note.pk])).content
        from apps.domains.registration_submission import _claim
        from unittest.mock import Mock
        submission_order = DomainOrder.objects.create(
            user=search_customer, domain_name="submission-ui.com", tld="com", status="processing",
            registration_contact=search_contact, admin_contact=search_contact, tech_contact=search_contact, billing_contact=search_contact,
        )
        submission_attempt, _ = _claim(submission_order.pk, "999", ["ns1.example.com", "ns2.example.com"], Mock())
        from apps.domains.models import DomainTransfer, DomainTransferAttempt
        transfer_fixture = DomainTransfer.objects.create(user=search_customer, domain_name="transfer-ui.com", tld="com", status="processing",
            auth_code="PRIVATE-TRANSFER-AUTH", last_error="PRIVATE-TRANSFER-DIAGNOSTIC", registration_contact=search_contact,
            admin_contact=search_contact, tech_contact=search_contact, billing_contact=search_contact)
        transfer_attempt = DomainTransferAttempt.objects.create(transfer=transfer_fixture)
        for transfer_state in ("preparing", "submitting", "unknown", "accepted", "aborted"):
            transfer_attempt.status = transfer_state
            transfer_attempt.save()
            response = client.get(reverse("admin_tools:domain_transfer_detail", args=[transfer_fixture.pk]))
            assert response.status_code == 200
            assert b"PRIVATE-TRANSFER" not in response.content
            fixtures[f"/admin-transfer-{transfer_state}"] = response.content
        transfer_attempt.status, transfer_attempt.legacy_review_required = "unknown", True
        transfer_attempt.save()
        fixtures["/admin-transfer-historical"] = client.get(reverse("admin_tools:domain_transfer_detail", args=[transfer_fixture.pk])).content
        fixtures["/admin-transfers"] = client.get(reverse("admin_tools:domain_transfers_list")).content
        fixtures["/admin-transfers-empty"] = client.get(reverse("admin_tools:domain_transfers_list") + "?state=accepted").content
        fixtures["/admin-transfers-invalid"] = client.get(reverse("admin_tools:domain_transfers_list") + "?state=invalid").content
        diagnostic_session = Mock()
        diagnostic_session.prepare_request.return_value.method = "GET"
        diagnostic_session.send.return_value.status_code = 200
        diagnostic_session.send.return_value.text = "PRIVATE-PROVIDER-DIAGNOSTIC"
        with patch("apps.admin_tools.views.requests.Session", return_value=diagnostic_session):
            fixtures["/admin-registrar-diagnostic"] = client.post(reverse("admin_tools:resellerclub_debug"), {}).content
            diagnostic_session.send.side_effect = RuntimeError("PRIVATE-PROVIDER-DIAGNOSTIC")
            fixtures["/admin-registrar-diagnostic-failed"] = client.post(reverse("admin_tools:resellerclub_debug"), {}).content
        transfer_customer_client = Client()
        transfer_customer_client.force_login(search_customer)
        fixtures["/portal-transfer-review"] = transfer_customer_client.get(reverse("domains:my_domains")).content
        with patch("apps.domains.tasks._build_nameservers", return_value=["ns1.example.com", "ns2.example.com"]):
            for submission_state in ("preparing", "submitting", "unknown", "accepted", "aborted"):
                submission_attempt.status = submission_state
                submission_attempt.remote_order_id = "501" if submission_state == "accepted" else ""
                submission_order.registrar_order_id = submission_attempt.remote_order_id
                submission_order.save(update_fields=["registrar_order_id"])
                submission_attempt.save(update_fields=["status", "remote_order_id", "updated_at"])
                response = client.get(reverse("admin_tools:domain_order_detail", args=[submission_order.pk]))
                assert response.status_code == 200
                fixtures[f"/admin-registration-{submission_state}"] = response.content
        from apps.domains.historical_registration import historical_subject
        from datetime import date
        submission_order.registrar_order_id = "PRIVATE-HISTORICAL-IDENTIFIER"
        submission_order.last_error = "PRIVATE-HISTORICAL-DIAGNOSTIC"
        submission_order.save(update_fields=["registrar_order_id", "last_error"])
        submission_attempt.status = "unknown"
        submission_attempt.legacy_review_required = True
        submission_attempt.subject = historical_subject(submission_order)
        submission_attempt.save()
        fixtures["/admin-historical-review"] = client.get(reverse("admin_tools:domain_order_detail", args=[submission_order.pk])).content
        fixtures["/admin-historical-list"] = client.get(reverse("admin_tools:domain_orders_list")).content
        customer_client = Client()
        customer_client.force_login(search_customer)
        held_domain = Domain.objects.create(user=search_customer, name=submission_order.domain_name, tld="com", status="active", registrar_id="501", epp_code="PRIVATE-CACHED-HISTORICAL-EPP")
        submission_order.domain = held_domain
        submission_order.save(update_fields=["domain"])
        fixtures["/portal-historical-review"] = customer_client.get(reverse("domains:my_domains")).content
        fixtures["/portal-historical-domain"] = customer_client.get(reverse("domains:detail", args=[held_domain.pk])).content
        held_response = customer_client.post(reverse("domains:get_auth_code", args=[held_domain.pk]))
        assert held_response.status_code == 409
        fixtures["/portal-historical-action-held"] = held_response.content
        submission_order.registrar_order_id = "501"
        submission_order.last_error = ""
        submission_order.save(update_fields=["registrar_order_id", "last_error"])
        for historical_state in ("pending", "suspended"):
            submission_attempt.status = "accepted"
            submission_attempt.legacy_review_required = False
            submission_attempt.registry_status = historical_state
            submission_attempt.registry_checked_at = timezone.now()
            submission_attempt.registry_registered_at = date(2020, 1, 1) if historical_state == "suspended" else None
            submission_attempt.registry_expires_at = date(2030, 1, 1) if historical_state == "suspended" else None
            submission_attempt.subject = {**submission_attempt.subject, "nameservers": [] if historical_state == "pending" else ["ns1.example.com", "ns2.example.com"]}
            submission_attempt.save()
            fixtures[f"/admin-historical-{historical_state}"] = client.get(reverse("admin_tools:domain_order_detail", args=[submission_order.pk])).content
        suggestion_payload = client.get(reverse("admin_tools:search_suggestions"), {"q": "global"}).content


        fixtures["/staff-roles"] = client.get(reverse("admin_tools:staff_roles")).content
        fixtures["/staff-roles-invalid"] = client.post(reverse("admin_tools:staff_roles"), {}).content
        fixtures["/branding"] = client.get(reverse("admin_tools:billing_branding")).content
        from django.core.files.uploadedfile import SimpleUploadedFile
        fixtures["/branding-invalid"] = client.post(reverse("admin_tools:billing_branding"), {
            "logo": SimpleUploadedFile("active.png", b"<svg>active</svg>"),
        }).content
        fixtures["/configuration"] = client.get(reverse("admin_tools:settings_overview")).content
        fixtures["/configuration-invalid"] = client.post(reverse("admin_tools:settings_import"), {
            "backup_password": "test-backup-password", "backup_file": SimpleUploadedFile("invalid.zip", b"not a zip"),
        }, follow=True).content
        from apps.core.models import PublicSubmission, PublicDelivery
        from django.utils import timezone
        for index, state in enumerate(("unknown", "failed")):
            receipt = PublicSubmission.objects.create(key=str(index) * 64, kind="contact", owner_digest="a" * 64, payload_digest="b" * 64)
            PublicDelivery.objects.create(submission=receipt, state=state, available_at=timezone.now(), attempts=3,
                                          total_attempts=3, error_code="delivery_uncertain" if state == "unknown" else "preparation_failed")
        fixtures["/deliveries"] = client.get(reverse("admin_tools:task_management")).content
        fixtures["/deliveries-invalid"] = client.post(reverse("admin_tools:task_management"), {}).content
        session = client.session
        session["_mfa_verified_at"] = 0
        session.save()
        fixtures["/reauth-required"] = client.post(reverse("admin_tools:settings_import"), {}).content
        agent = get_user_model().objects.create_user(email="support@example.test", password="test-password", is_staff=True)
        agent.groups.add(Group.objects.get(name="Portal Support"))
        client.force_login(agent)
        fixtures["/admin-customer-workspace-support"] = client.get(workspace_url).content
        billing_agent = get_user_model().objects.create_user(email="billing-ui@example.test", password="test-password", is_staff=True)
        billing_agent.groups.add(Group.objects.get(name="Portal Billing"))
        client.force_login(billing_agent)
        fixtures["/admin-customer-workspace-billing"] = client.get(workspace_url).content
        client.force_login(agent)
        fixtures["/staff-workspace"] = client.get(reverse("admin_tools:dashboard")).content
        fixtures["/staff-customers"] = client.get(reverse("admin_tools:users")).content
        fixtures["/admin-search-support"] = client.get(reverse("admin_tools:search"), {"q": "global"}).content
        long_search_domain = Domain.objects.create(user=search_customer, name='globalsearch.' + ('a' * 60 + '.') * 3 + 'example', tld='example', epp_code='NOT-FOR-SEARCH')
        fixtures["/admin-search-record"] = client.get(reverse("admin_tools:search_record", args=["domains", long_search_domain.pk])).content

        from apps.billing.models import Quote
        from apps.billing.services import create_quote, LineItemSpec
        from decimal import Decimal
        confirmation_quote = create_quote(user=None, line_items=[LineItemSpec(description="Fixture hosting", quantity=Decimal(1), unit_price=Decimal(10))], status=Quote.STATUS_SENT)
        confirmation_url = reverse("billing_public:quote_public_accept_continue", args=[confirmation_quote.public_token])
        fixtures["/public-acceptance"] = client.get(confirmation_url).content
        from apps.core.public_submissions import SubmissionUnavailable
        with patch("apps.billing.acceptance.accept_public_quote", side_effect=SubmissionUnavailable("Unavailable")):
            fixtures["/public-acceptance-unavailable"] = client.post(confirmation_url).content
        confirmation_quote.status = Quote.STATUS_EXPIRED
        confirmation_quote.save(update_fields=["status"])
        fixtures["/public-acceptance-expired"] = client.get(confirmation_url).content


    class FixtureHandler(SimpleHTTPRequestHandler):
        def __init__(self, *values, **kwargs):
            super().__init__(*values, directory=str(root), **kwargs)

        def do_GET(self):
            if self.path in fixtures:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(fixtures[self.path])
            elif self.path.startswith("/static/"):
                super().do_GET()
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 960})
                for name in ("publication-redaction-review", "publication-redaction-unknown", "publication-redaction-awaiting", "publication-redaction-confirmed", "portal-contact-change-form", "portal-contact-change-invalid", "portal-contact-change-queued", "portal-contact-change-awaiting", "portal-contact-change-unknown", "portal-contact-change-confirmed", "portal-contact-change-review", "registry-me-registration", "registry-me-invalid", "registry-uk-domain", "registry-uk-operation", "registry-me-renewal-review", "registry-me-held-domain", "registry-me-domain-list", "publication-registration", "publication-empty", "publication-shop", "publication-cart", "publication-active", "publication-stale-cart", "publication-withdrawn", "publication-provider-pending", "admin-registrar-diagnostic", "admin-registrar-diagnostic-failed", "admin-transfer-preparing", "admin-transfer-submitting", "admin-transfer-unknown", "admin-transfer-accepted", "admin-transfer-aborted", "admin-transfer-historical", "admin-transfers", "admin-transfers-empty", "admin-transfers-invalid", "portal-transfer-review", "portal-historical-domain", "portal-historical-action-held", "portal-historical-review", "admin-historical-review", "admin-historical-list", "admin-historical-pending", "admin-historical-suspended", "admin-registration-preparing", "admin-registration-submitting", "admin-registration-unknown", "admin-registration-accepted", "admin-registration-aborted", "admin-customer-workspace", "admin-customer-workspace-loading", "admin-customer-workspace-invalid", "admin-customer-workspace-empty", "admin-customer-workspace-support", "admin-customer-workspace-billing", "admin-customer-note-redacted", "portal-cart-empty", "portal-cart", "public-cart-review", "registrant-verification-automatic", "registrant-verification-queued", "registrant-verification-error", "registrant-verification-review", "registrant-verification-verified", "registrant-verification-pending", "registrant-verification-suspended", "registrant-verification-unknown", "registrant-domain-uk", "registrant-domain-review", "admin-manual-domain", "admin-manual-domain-invalid", "registrant-contact", "registrant-contact-invalid", "mfa", "mfa-invalid", "reauthenticate", "reauth-required", "staff-roles", "staff-roles-invalid", "staff-workspace", "staff-customers", "branding", "branding-invalid", "configuration", "configuration-invalid", "deliveries", "deliveries-invalid", "public-contact", "public-contact-invalid", "public-error-413", "public-error-429", "public-error-503", "public-quote", "public-domain-error", "public-acceptance", "public-acceptance-unavailable", "public-acceptance-expired", "public-mail-reset", "public-mail-verification", "admin-customers", "admin-customers-empty", "admin-customers-loading", "admin-user-invalid", "admin-invoices", "admin-invoices-empty", "admin-search", "admin-search-start", "admin-search-suggestions-error", "admin-search-empty", "admin-search-invalid", "admin-search-pagination", "admin-search-support", "admin-search-record"):
                    if args.pages and name not in args.pages:
                        continue
                    pending_routes = []
                    page.goto(f"http://127.0.0.1:{server.server_port}/{name}")
                    if name == "admin-customer-workspace-loading":
                        page.route("**/admin-tools/customers/*/notes/add/", lambda route: pending_routes.append(route))
                        page.evaluate("() => { const target = document.createElement('iframe'); target.name = 'note-post-target'; target.hidden = true; document.body.appendChild(target); document.querySelector('.admin-note-form').target = target.name; }")
                        page.locator('#id_body').fill('Preserve this internal note while saving.')
                        page.locator('.admin-note-form').evaluate('form => form.requestSubmit()')
                        page.locator('#note-saving-status').wait_for(state='visible')
                        assert page.locator('.admin-note-form').get_attribute('aria-busy') == 'true'
                        page.locator('.admin-note-form').evaluate('form => form.requestSubmit()')
                        assert len(pending_routes) == 1
                        assert page.locator('#id_body').input_value() == 'Preserve this internal note while saving.'
                    if name == "admin-customers-loading":
                        page.route("**/admin-customers-loading", lambda route: pending_routes.append(route))
                        # Keep the fixture document inspectable while a real
                        # native form POST is pending in an isolated target.
                        page.evaluate("() => { const target = document.createElement('iframe'); target.name = 'fixture-post-target'; target.hidden = true; document.body.appendChild(target); document.querySelector('#whm-sync-form').target = target.name; }")
                        page.locator('#whm-sync-form').evaluate('form => form.requestSubmit()')
                        page.locator('#whm-sync-progress').wait_for(state='visible')
                        assert page.locator('#whm-sync-form').get_attribute('aria-busy') == 'true'
                        assert len(pending_routes) == 1
                        assert 'action=sync_whm' in pending_routes[0].request.post_data
                        assert page.locator('#whm-sync-progress').evaluate("el => { const banner = document.getElementById('cookie-banner'); return !banner || !banner.getClientRects().length || el.getBoundingClientRect().bottom <= banner.getBoundingClientRect().top; }")
                    if name in {"admin-search-start", "admin-search-suggestions-error"}:
                        suggestions = []
                        if name == "admin-search-start":
                            page.route("**/search/suggestions/**", lambda route: suggestions.append(route))
                        else:
                            page.route("**/search/suggestions/**", lambda route: route.fulfill(status=503, content_type="application/json", body='{"error":"Temporarily unavailable"}'))
                        page.locator('#id_q').fill('global')
                        if name == "admin-search-start":
                            page.wait_for_function("document.querySelector('[data-search-status]').textContent === 'Finding suggestions.'")
                            # One round trip lets the intercepted fetch arrive.
                            page.locator('#id_q').input_value()
                            assert len(suggestions) == 1
                            suggestions[0].fulfill(status=200, content_type="application/json", body=suggestion_payload)
                            page.wait_for_function("document.querySelector('#search-suggestions').children.length > 0")
                        else:
                            page.wait_for_function("document.querySelector('[data-search-status]').textContent.includes('unavailable')")
                        assert page.locator('#id_q').input_value() == 'global'
                        assert page.locator('#id_q').evaluate('el => el === document.activeElement')
                        page.unroute("**/search/suggestions/**")
                    if name == "admin-search-support":
                        assert set(page.locator('.admin-search-group-heading h2').all_text_contents()) == {'Customers', 'Domains', 'Hosting services', 'Support tickets'}
                    if name == "admin-search-record":
                        assert page.get_by_text('Manage this record', exact=True).count() == 0
                        assert 'NOT-FOR-SEARCH' not in page.locator('.admin-content').inner_text()
                    if name.startswith("registrant-verification-") and name != "registrant-verification-error":
                        refresh = page.get_by_role('button', name='Queue status check', exact=True)
                        assert refresh.count() == 1
                        assert refresh.locator('xpath=..').get_attribute('method') == 'post'
                        assert refresh.locator('xpath=..').locator('input[name=csrfmiddlewaretoken]').input_value()
                        if name in {"registrant-verification-pending", "registrant-verification-suspended"}:
                            assert page.get_by_role('button', name='Request verification email', exact=True).count() == 1
                        if name in {"registrant-verification-verified", "registrant-verification-review"}:
                            assert page.get_by_role('button', name='Request verification email', exact=True).count() == 0
                        if name == "registrant-verification-review":
                            assert page.get_by_text('An earlier email request has an uncertain outcome.', exact=False).count() == 1
                        if name == "registrant-verification-automatic":
                            assert page.get_by_role('status').filter(has_text='Automatic status check: Queued').count() == 1
                    if name.startswith("public-error"):
                        page.wait_for_function("getComputedStyle(document.querySelector('.request-error')).backgroundColor === 'rgb(255, 255, 255)'")
                    elif name.startswith(("public-", "registrant-", "portal-", "publication-", "registry-")):
                        page.wait_for_function("typeof tailwind !== 'undefined'")
                    else:
                        page.wait_for_function("getComputedStyle(document.querySelector('.auth-card, .admin-header')).backgroundColor === 'rgb(255, 255, 255)'")
                    if name == "publication-registration":
                        assert page.locator('#id_publication_choice').input_value() == 'redact'
                        assert page.locator('#id_publication_choice_helptext').count() == 1
                        assert page.locator('form[data-no-field-hints]').count() == 1
                    if name == "publication-shop":
                        for choice_id in ('registration-publication', 'transfer-publication'):
                            assert page.locator('#' + choice_id).input_value() == 'redact'
                        assert page.locator('[name=publication_choice]').count() == 2
                        assert page.evaluate("() => { const ids = [...document.querySelectorAll('[id]')].map(el => el.id); return ids.length === new Set(ids).size; }")
                    if name == "publication-active":
                        button = page.get_by_role('button', name='Withdraw publication consent for publication-ui.com', exact=True)
                        assert button.locator('xpath=..').get_attribute('method') == 'post'
                        assert button.locator('xpath=..').locator('[name=csrfmiddlewaretoken]').input_value()
                    if name == "publication-withdrawn":
                        assert page.get_by_role('button', name='Withdraw publication consent', exact=False).count() == 0
                    if name == "publication-provider-pending":
                        assert page.get_by_role('link', name='View redaction support request', exact=True).count() == 1
                    if name == "public-quote":
                        # Exercise the visible consent choice before controls
                        # behind the fixed banner, as a visitor would.
                        if page.locator('#cookie-reject').is_visible():
                            page.locator('#cookie-reject').click()
                        page.get_by_role('button', name='Add Fixture hosting', exact=True).click()
                        page.get_by_role('button', name='Remove Fixture hosting (monthly)', exact=True).click()
                        page.get_by_role('button', name='Add Fixture hosting', exact=True).click()
                        assert page.locator('[x-data]').evaluate("el => { const cart = Alpine.$data(el).cart; return cart.length === 2 && cart.filter(line => line.kind === 'setup').length === 1 && cart.every(line => line.quantity === 1); }")
                        page.route("**/submit/", lambda route: route.fulfill(status=429, content_type="application/json", body=json.dumps({"error": "Too many requests.", "retry_after": 60})))
                        page.locator('#quote-name').fill('Example customer')
                        page.locator('#quote-email').fill('example@example.test')
                        page.get_by_role('button', name='Get my quote').click()
                        page.get_by_role('alert').filter(has_text='60 seconds').wait_for()
                        # Alpine re-enables the button after the error. Check
                        # its settled state rather than a disabled-to-active
                        # colour transition partway through an animation frame.
                        page.get_by_role('button', name='Get my quote').evaluate("async el => { await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame); await Promise.allSettled(el.getAnimations().map(animation => animation.finished)); }")
                        assert page.locator('#quote-name').input_value() == 'Example customer'
                    if name == "public-domain-error":
                        page.route("**/check/**", lambda route: route.fulfill(status=429, headers={"X-Public-Request-Error": "1", "Content-Type": "application/json"}, body=json.dumps({"error": "Please wait <script>unsafe</script>", "retry_after": 60})))
                        page.locator('#domain-search-input').fill('example')
                        page.locator('.public-request-error').wait_for()
                        assert page.locator('#domain-search-input').input_value() == 'example'
                        assert page.locator('#domain-search-input').evaluate('el => el === document.activeElement')
                        assert page.locator('.public-request-error script').count() == 0
                    if name.startswith(("staff-", "branding", "configuration", "deliveries")):
                        page.wait_for_function("document.querySelector('.admin-header h2').textContent.length > 0")
                    if name == "deliveries":
                        page.get_by_text("Review a delivery", exact=True).click()
                    page.add_script_tag(path=str(args.axe.resolve()))
                    result = page.evaluate("""async () => await axe.run(document.querySelector('.auth-shell, .admin-shell, main') || document.body, {
                        runOnly: {type: 'rule', values: ['color-contrast', 'label', 'button-name', 'link-name', 'aria-valid-attr-value']}
                    })""")
                    violations = [{"rule": item["id"], "nodes": [n["html"] for n in item["nodes"]]} for item in result["violations"]]
                    assert not violations, (name, width, violations)
                    if name == "portal-cart":
                        assert page.get_by_role('heading', name='Your cart', level=1).count() == 1
                        assert page.get_by_text('Remove', exact=True).get_attribute('aria-label').startswith('Remove Domain registration:')
                        invoice_button = page.get_by_role('button', name='Create invoice', exact=True)
                        invoice_button.evaluate("el => el.scrollIntoView({block: 'end'})")
                        page.wait_for_function("() => { const button = Array.from(document.querySelectorAll('button')).find(el => el.textContent.trim() === 'Create invoice'); const banner = document.querySelector('#cookie-banner'); return !banner || !banner.getBoundingClientRect().height || button.getBoundingClientRect().bottom <= banner.getBoundingClientRect().top; }")
                    if name == "public-cart-review":
                        page.get_by_role('link', name='Manage domain contacts', exact=True).focus()
                        page.keyboard.press('Tab')
                        assert page.get_by_role('link', name='Return to quote', exact=False).evaluate('el => el === document.activeElement')
                    if name.startswith("registrant-") and not name.startswith("registrant-verification-"):
                        submit = page.locator('main button[type="submit"]').first
                        submit.evaluate("el => el.scrollIntoView({block: 'end'})")
                        page.wait_for_function("() => { const submit = document.querySelector('main button[type=submit]'); const banner = document.querySelector('#cookie-banner'); return !banner || !banner.getBoundingClientRect().height || submit.getBoundingClientRect().bottom <= banner.getBoundingClientRect().top; }")
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (name, width, page.evaluate("Array.from(document.querySelectorAll('.admin-main, .admin-content, .admin-table-scroll')).map(el => ({element: el.className, scroll: el.scrollWidth, client: el.clientWidth, overflow: getComputedStyle(el).overflow, position: getComputedStyle(el).position})).concat(Array.from(document.body.querySelectorAll('*')).filter(el => el.getBoundingClientRect().right > innerWidth && !el.closest('.admin-table-scroll')).slice(0,10).map(el => ({element: el.outerHTML.slice(0,150), right: el.getBoundingClientRect().right})))"))
                    if name.startswith("public-mail-"):
                        assert "queued for delivery" in page.locator('.auth-card').inner_text()
                    if name == "public-acceptance":
                        page.get_by_role('button', name='Accept quote and create invoice').focus()
                        page.keyboard.press('Tab')
                        assert page.get_by_role('link', name='Review quote details').evaluate('el => el === document.activeElement')
                    if name.startswith("public-error"):
                        page.get_by_role('link', name='Open a fresh page').focus()
                        page.keyboard.press('Tab')
                        assert page.get_by_role('link', name='Go to home page').evaluate('el => el === document.activeElement')
                    if name.startswith("mfa"):
                        page.locator("#mfa-code").focus()
                        page.keyboard.press("Tab")
                        assert page.locator('button[type="submit"]').evaluate("element => element === document.activeElement")
                    if name == "reauthenticate":
                        page.locator("#id_password").focus()
                        page.keyboard.press("Tab")
                        assert page.locator("#id_code").evaluate("element => element === document.activeElement")
                    if name == "admin-search-invalid":
                        page.locator('[data-admin-error-summary] a').first.click()
                        assert page.locator('#id_q').evaluate('el => el === document.activeElement')
                    if name == "admin-search":
                        page.locator('#id_q').focus()
                        page.keyboard.press('Tab')
                        assert page.get_by_role('button', name='Search', exact=True).evaluate('el => el === document.activeElement')
                    if name == "admin-customer-workspace-invalid":
                        assert page.locator('[data-admin-error-summary]').evaluate('el => el === document.activeElement')
                        page.locator('[data-admin-error-summary] a').first.click()
                        assert page.locator('#id_body').evaluate('el => el === document.activeElement')
                    if name.startswith("admin-customer-workspace"):
                        assert page.get_by_role('heading', name='Customer workspace', level=1).count() == 1
                        if name.endswith("-billing"):
                            assert page.get_by_role('heading', name='Contact validation', exact=True).count() == 0
                            assert page.get_by_role('button', name='Add internal note', exact=True).count() == 0
                        if name.endswith("-support"):
                            assert page.get_by_role('heading', name='Outstanding balances', exact=True).count() == 0
                            assert page.get_by_text('Multi-factor authentication', exact=True).count() == 0
                    if name == "admin-user-invalid":
                        assert page.locator('[data-admin-error-summary]').evaluate('el => el === document.activeElement')
                        page.locator('[data-admin-error-summary] a').first.click()
                        assert page.locator('#id_email').evaluate('el => el === document.activeElement')
                    if name in {"registrant-contact", "registrant-contact-invalid"}:
                        country = page.locator('#id_country')
                        assert country.locator('option').count() == 250
                        assert country.locator('option[value="GB"]').count() == 1
                        assert country.locator('option[value="UK"]').count() == 0
                        assert page.locator('[data-no-field-hints] .field-example-hint').count() == 0
                        if name.endswith('-invalid'):
                            assert page.locator('[data-contact-error-summary]').evaluate('el => el === document.activeElement')
                            page.locator('[data-contact-error-summary] a[href="#id_country"]').click()
                            assert country.evaluate('el => el === document.activeElement')
                        country.focus()
                        page.keyboard.press('End')
                        assert country.input_value() == 'ZW'
                        if width == 390:
                            assert country.bounding_box()['width'] >= 200
                        page.screenshot(path=str(args.output_dir / f"{name}-country-{width}.png"), full_page=True)
                    if name == "admin-manual-domain-invalid":
                        assert page.locator('#contact_country').input_value() == 'ZZ'
                        assert page.locator('#contact_country option:checked').text_content().startswith('Unsupported code')
                    if name.startswith('admin-registration-'):
                        assert page.get_by_role('heading', name='Domain registration order', level=1).count() == 1
                        assert page.get_by_role('heading', name='Registrar submission', level=2).count() == 1
                        assert page.get_by_role('button', name='Delete order', exact=True).count() == 0
                        if name.endswith(('preparing', 'submitting', 'unknown')):
                            assert page.get_by_role('button', name='Process / register', exact=True).count() == 0
                            assert page.locator('#order-domain_name').is_disabled()
                        if name.endswith('accepted'):
                            assert page.get_by_role('button', name='Resume local completion', exact=True).count() == 1
                        if name.endswith('aborted'):
                            assert page.locator('#order-domain_name').is_enabled()
                    if name.startswith('admin-historical-'):
                        assert 'PRIVATE-HISTORICAL' not in page.locator('body').text_content()
                        if name.endswith('review'):
                            assert page.get_by_text('Historical submission not confirmed.', exact=False).count() == 1
                            assert page.locator('#order-domain_name').is_disabled()
                            assert page.get_by_role('button', name='Process / register', exact=True).count() == 0
                        if name.endswith('pending'):
                            assert page.get_by_text('Not reported', exact=True).count() == 2
                            assert page.get_by_text('Delegation was not reported in the last provider snapshot.', exact=True).count() == 1
                        if name.endswith('suspended'):
                            assert page.get_by_text('Suspended', exact=True).count() == 1
                            assert page.get_by_text('01 Jan 2020', exact=True).count() == 1
                    if name == 'portal-historical-review':
                        assert page.get_by_role('heading', name='Registration awaiting review', level=2).count() == 1
                        assert 'PRIVATE-HISTORICAL' not in page.locator('body').text_content()
                        assert page.get_by_role('link', name='Contact support about registration status', exact=True).count() == 1
                        if width == 390:
                            region = page.get_by_role('region', name='Domain records', exact=True)
                            region.focus()
                            page.keyboard.press('ArrowRight')
                            page.wait_for_function("document.querySelector('[aria-label=\"Domain records\"]').scrollLeft > 0")
                    if name == 'portal-historical-domain':
                        assert page.get_by_role('heading', name='Registrar association awaiting review', level=2).count() == 1
                        assert 'PRIVATE-CACHED-HISTORICAL-EPP' not in page.locator('body').text_content()
                    if name == 'portal-historical-action-held':
                        assert page.get_by_role('heading', name='Registrar review required', level=1).count() == 1
                    if name.startswith('admin-'):
                        page.screenshot(path=str(args.output_dir / f"{name}-overview-{width}.png"), full_page=True)
                    if name in {"admin-customers", "admin-invoices"} and width == 390:
                        region = page.locator('.admin-table-scroll')
                        assert region.evaluate('el => el.scrollWidth > el.clientWidth')
                        region.focus()
                        page.keyboard.press('ArrowRight')
                        page.wait_for_function("document.querySelector('.admin-table-scroll').scrollLeft > 0")
                    page.screenshot(path=str(args.output_dir / f"{name}-{width}.png"), full_page=True)
                    if name == "admin-customer-workspace":
                        page.locator('#contact-checks').scroll_into_view_if_needed()
                        page.screenshot(path=str(args.output_dir / f"{name}-validation-{width}.png"), full_page=True)
                        page.locator('#internal-notes').scroll_into_view_if_needed()
                        page.screenshot(path=str(args.output_dir / f"{name}-notes-{width}.png"), full_page=True)
                        button = page.get_by_role('button', name='Add internal note', exact=True)
                        button.scroll_into_view_if_needed()
                        assert button.evaluate("el => { const banner = document.getElementById('cookie-banner'); return !banner || !banner.getClientRects().length || el.getBoundingClientRect().bottom <= banner.getBoundingClientRect().top; }")
                    if name == "admin-customers" and width == 390:
                        opener = page.get_by_role('button', name='Open menu', exact=True)
                        opener.click()
                        page.wait_for_function("document.querySelector('#admin-navigation').getAttribute('aria-modal') === 'true'")
                        page.locator('#admin-navigation').evaluate("async el => { await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame); await Promise.allSettled(el.getAnimations().map(animation => animation.finished)); }")
                        assert page.locator('#admin-navigation').evaluate('el => Math.abs(el.getBoundingClientRect().left) < 1 && el.getBoundingClientRect().right <= innerWidth')
                        assert page.locator('.admin-main').evaluate('el => el.inert')
                        assert page.get_by_role('button', name='Close menu', exact=True).evaluate('el => el === document.activeElement')
                        page.keyboard.press('Shift+Tab')
                        assert page.get_by_role('link', name='Sign out', exact=True).evaluate('el => el === document.activeElement')
                        page.keyboard.press('Tab')
                        assert page.get_by_role('button', name='Close menu', exact=True).evaluate('el => el === document.activeElement')
                        drawer_result = page.evaluate("async () => await axe.run(document.querySelector('#admin-navigation'), {runOnly: {type: 'rule', values: ['color-contrast', 'button-name', 'link-name', 'aria-valid-attr-value']}})")
                        assert not drawer_result['violations'], drawer_result['violations']
                        page.screenshot(path=str(args.output_dir / 'admin-drawer-390.png'), full_page=True)
                        page.keyboard.press('Escape')
                        assert opener.evaluate('el => el === document.activeElement')
                        assert page.locator('#admin-navigation').evaluate('el => el.inert')
                        assert not page.locator('.admin-main').evaluate('el => el.inert')
                        opener.click()
                        page.get_by_role('button', name='Close menu', exact=True).click()
                        assert opener.evaluate('el => el === document.activeElement')
                        opener.click()
                        page.locator('.admin-drawer-backdrop').click(position={'x': 380, 'y': 200})
                        assert opener.evaluate('el => el === document.activeElement')
                        opener.click()
                        page.set_viewport_size({'width': 1440, 'height': 960})
                        page.wait_for_function("!document.querySelector('#admin-navigation').inert && !document.querySelector('#admin-navigation').hasAttribute('aria-modal')")
                        page.wait_for_function("document.querySelector('#admin-navigation').contains(document.activeElement)")
                        page.set_viewport_size({'width': 390, 'height': 960})
                        page.wait_for_function("document.querySelector('#admin-navigation').inert")
                        page.wait_for_function("document.activeElement === document.querySelector('[aria-label=\"Open menu\"]')")
                    for pending in pending_routes:
                        pending.abort()
                    if pending_routes:
                        page.unroute("**/admin-customers-loading")
                    reports.append({"page": name, "width": width, "violations": violations})
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    assert reports, "No browser checks ran; check the requested fixture names."
    if args.pages:
        assert set(args.pages) <= {report["page"] for report in reports}, "Some requested fixture pages were not checked."
    (args.output_dir / "results.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
    print(json.dumps({"checks": len(reports), "output": str(args.output_dir)}, indent=2))


if __name__ == "__main__":
    main()
