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
                for name in ("mfa", "mfa-invalid", "reauthenticate", "reauth-required", "staff-roles", "staff-roles-invalid", "staff-workspace", "staff-customers", "branding", "branding-invalid", "configuration", "configuration-invalid", "deliveries", "deliveries-invalid", "public-contact", "public-contact-invalid", "public-error-413", "public-error-429", "public-error-503", "public-quote", "public-domain-error", "public-acceptance", "public-acceptance-unavailable", "public-acceptance-expired", "public-mail-reset", "public-mail-verification", "admin-customers", "admin-customers-empty", "admin-customers-loading", "admin-user-invalid", "admin-invoices", "admin-invoices-empty", "admin-search", "admin-search-start", "admin-search-suggestions-error", "admin-search-empty", "admin-search-invalid", "admin-search-pagination", "admin-search-support", "admin-search-record"):
                    if args.pages and name not in args.pages:
                        continue
                    pending_routes = []
                    page.goto(f"http://127.0.0.1:{server.server_port}/{name}")
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
                    if name.startswith("public-error"):
                        page.wait_for_function("getComputedStyle(document.querySelector('.request-error')).backgroundColor === 'rgb(255, 255, 255)'")
                    elif name.startswith("public-"):
                        page.wait_for_function("typeof tailwind !== 'undefined'")
                    else:
                        page.wait_for_function("getComputedStyle(document.querySelector('.auth-card, .admin-header')).backgroundColor === 'rgb(255, 255, 255)'")
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
                    if name == "admin-user-invalid":
                        assert page.locator('[data-admin-error-summary]').evaluate('el => el === document.activeElement')
                        page.locator('[data-admin-error-summary] a').first.click()
                        assert page.locator('#id_email').evaluate('el => el === document.activeElement')
                    if name.startswith('admin-'):
                        page.screenshot(path=str(args.output_dir / f"{name}-overview-{width}.png"), full_page=True)
                    if name in {"admin-customers", "admin-invoices"} and width == 390:
                        region = page.locator('.admin-table-scroll')
                        assert region.evaluate('el => el.scrollWidth > el.clientWidth')
                        region.focus()
                        page.keyboard.press('ArrowRight')
                        page.wait_for_function("document.querySelector('.admin-table-scroll').scrollLeft > 0")
                    page.screenshot(path=str(args.output_dir / f"{name}-{width}.png"), full_page=True)
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
    (args.output_dir / "results.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
    print(json.dumps({"checks": len(reports), "output": str(args.output_dir)}, indent=2))


if __name__ == "__main__":
    main()
