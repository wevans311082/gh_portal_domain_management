from django.urls import path
from . import views
from . import wizard_views
from . import billing_views
from . import content_views
from . import operations_views

app_name = "admin_tools"

urlpatterns = [
    # Overview
    path("", views.dashboard, name="dashboard"),
    path("stats/", views.stats, name="stats"),
    # People
    path("users/", views.users, name="users"),
    path("users/new/", content_views.user_create, name="user_create"),
    path("users/<int:pk>/", content_views.user_edit, name="user_edit"),
    path("users/<int:pk>/mfa/", content_views.user_mfa_manage, name="user_mfa_manage"),
    path("users/<int:pk>/su/", content_views.user_su_start, name="user_su_start"),
    path("users/su/stop/", content_views.user_su_stop, name="user_su_stop"),
    path("users/company-lookup/", content_views.company_lookup, name="company_lookup"),
    # Commerce
    path("invoices/", views.invoices, name="invoices"),
    # Billing workbench
    path("billing/branding/", billing_views.branding_edit, name="billing_branding"),
    path("billing/invoices/", billing_views.invoice_list, name="invoice_list"),
    path("billing/invoices/new/", billing_views.invoice_create, name="invoice_create"),
    path("billing/invoices/<int:pk>/", billing_views.invoice_edit, name="invoice_edit"),
    path("billing/invoices/<int:pk>/pdf/", billing_views.invoice_pdf, name="invoice_pdf"),
    path("billing/invoices/<int:pk>/<str:action>/", billing_views.invoice_action, name="invoice_action"),
    path("billing/quotes/", billing_views.quote_list, name="quote_list"),
    path("billing/quotes/new/", billing_views.quote_create, name="quote_create"),
    path("billing/quotes/<int:pk>/", billing_views.quote_edit, name="quote_edit"),
    path("billing/quotes/<int:pk>/pdf/", billing_views.quote_pdf, name="quote_pdf"),
    path("billing/quotes/<int:pk>/<str:action>/", billing_views.quote_action, name="quote_action"),
    path("domains/pricing/", views.tld_pricing, name="tld_pricing"),
    path("domains/", operations_views.domains_list, name="domains_list"),
    path("domains/new/", operations_views.domains_create, name="domains_create"),
    path("domains/<int:pk>/", operations_views.domains_edit, name="domains_edit"),
    path("domains/<int:pk>/delete/", operations_views.domains_delete, name="domains_delete"),
    path("services/", operations_views.services_list, name="services_list"),
    path("services/new/", operations_views.services_create, name="services_create"),
    path("services/<int:pk>/", operations_views.services_edit, name="services_edit"),
    path("services/<int:pk>/delete/", operations_views.services_delete, name="services_delete"),
    path("support/", operations_views.tickets_list, name="tickets_list"),
    path("support/new/", operations_views.tickets_create, name="tickets_create"),
    path("support/<int:pk>/", operations_views.tickets_edit, name="tickets_edit"),
    path("support/<int:pk>/delete/", operations_views.tickets_delete, name="tickets_delete"),
    path("payments/", operations_views.payments_list, name="payments_list"),
    path("payments/new/", operations_views.payments_create, name="payments_create"),
    path("payments/<int:pk>/", operations_views.payments_edit, name="payments_edit"),
    path("payments/<int:pk>/delete/", operations_views.payments_delete, name="payments_delete"),
    path("templates/", operations_views.templates_list, name="templates_list"),
    path("templates/new/", operations_views.templates_create, name="templates_create"),
    path("templates/<int:pk>/", operations_views.templates_edit, name="templates_edit"),
    path("templates/<int:pk>/delete/", operations_views.templates_delete, name="templates_delete"),
    # Contact submissions
    path("contact/submissions/", operations_views.contact_submissions_list, name="contact_submissions_list"),
    path("contact/submissions/<int:pk>/", operations_views.contact_submission_detail, name="contact_submission_detail"),
    path("contact/submissions/<int:pk>/delete/", operations_views.contact_submission_delete, name="contact_submission_delete"),
    path("contact/config/", operations_views.contact_form_config, name="contact_form_config"),
    # System
    path("tasks/", views.task_management, name="task_management"),
    path("templates/scan/", views.template_scan, name="template_scan"),
    path("integrations/", views.integrations_overview, name="integrations_overview"),
    path("integrations/<str:service>/", views.integration_detail, name="integration_detail"),
    path("integrations/companies-house/config/", views.companies_house_config, name="companies_house_config"),
    path("integrations/resellerclub/debug/", views.resellerclub_debug, name="resellerclub_debug"),
    path("security/", views.security, name="security"),
    path("database/", views.database, name="database"),
    path("settings/", views.settings_overview, name="settings_overview"),
    path("settings/setup/<str:step_key>/", views.settings_setup_step, name="settings_setup_step"),
    path("setup/", views.setup, name="setup"),
    # Setup wizard
    path("setup/wizard/", wizard_views.wizard_index, name="wizard_index"),
    path("setup/wizard/<str:step_key>/", wizard_views.wizard_step, name="wizard_step"),
    path("setup/wizard/reset/", wizard_views.wizard_reset, name="wizard_reset"),
    # Website content CMS
    path("content/", content_views.content_dashboard, name="content_dashboard"),
    path("content/settings/", content_views.content_settings_edit, name="content_settings"),
    path("content/faqs/", content_views.faq_list, name="faq_list"),
    path("content/faqs/new/", content_views.faq_create, name="faq_create"),
    path("content/faqs/<int:pk>/", content_views.faq_edit, name="faq_edit"),
    path("content/faqs/<int:pk>/delete/", content_views.faq_delete, name="faq_delete"),
    path("content/service-cards/", content_views.service_card_list, name="service_card_list"),
    path("content/service-cards/new/", content_views.service_card_create, name="service_card_create"),
    path("content/service-cards/<int:pk>/", content_views.service_card_edit, name="service_card_edit"),
    path("content/service-cards/<int:pk>/delete/", content_views.service_card_delete, name="service_card_delete"),
    path("content/package-cards/", content_views.package_card_list, name="package_card_list"),
    path("content/package-cards/<int:pk>/", content_views.package_card_edit, name="package_card_edit"),
    path("content/legal/", content_views.legal_page_list, name="legal_page_list"),
    path("content/legal/new/", content_views.legal_page_create, name="legal_page_create"),
    path("content/legal/<int:pk>/", content_views.legal_page_edit, name="legal_page_edit"),
    path("content/legal/<int:pk>/delete/", content_views.legal_page_delete, name="legal_page_delete"),
    path("content/errors/", content_views.error_page_list, name="error_page_list"),
    path("content/errors/new/", content_views.error_page_create, name="error_page_create"),
    path("content/errors/<int:pk>/", content_views.error_page_edit, name="error_page_edit"),
    path("content/errors/<int:pk>/delete/", content_views.error_page_delete, name="error_page_delete"),
]
