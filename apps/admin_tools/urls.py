from django.urls import path
from . import views
from . import wizard_views
from . import billing_views

app_name = "admin_tools"

urlpatterns = [
    # Overview
    path("", views.dashboard, name="dashboard"),
    path("stats/", views.stats, name="stats"),
    # People
    path("users/", views.users, name="users"),
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
    # System
    path("tasks/", views.task_management, name="task_management"),
    path("templates/scan/", views.template_scan, name="template_scan"),
    path("integrations/", views.integrations_overview, name="integrations_overview"),
    path("integrations/<str:service>/", views.integration_detail, name="integration_detail"),
    path("integrations/resellerclub/debug/", views.resellerclub_debug, name="resellerclub_debug"),
    path("security/", views.security, name="security"),
    path("database/", views.database, name="database"),
    path("settings/", views.settings_overview, name="settings_overview"),
    path("setup/", views.setup, name="setup"),
    # Setup wizard
    path("setup/wizard/", wizard_views.wizard_index, name="wizard_index"),
    path("setup/wizard/<str:step_key>/", wizard_views.wizard_step, name="wizard_step"),
    path("setup/wizard/reset/", wizard_views.wizard_reset, name="wizard_reset"),
]
