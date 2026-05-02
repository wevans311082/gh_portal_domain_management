from django.urls import path
from . import views
from . import wizard_views

app_name = "admin_tools"

urlpatterns = [
    # Overview
    path("", views.dashboard, name="dashboard"),
    path("stats/", views.stats, name="stats"),
    # People
    path("users/", views.users, name="users"),
    # Commerce
    path("invoices/", views.invoices, name="invoices"),
    # System
    path("tasks/", views.task_management, name="task_management"),
    path("templates/scan/", views.template_scan, name="template_scan"),
    path("integrations/", views.integrations_overview, name="integrations_overview"),
    path("integrations/<str:service>/", views.integration_detail, name="integration_detail"),
    path("security/", views.security, name="security"),
    path("database/", views.database, name="database"),
    path("settings/", views.settings_overview, name="settings_overview"),
    path("setup/", views.setup, name="setup"),
    # Setup wizard
    path("setup/wizard/", wizard_views.wizard_index, name="wizard_index"),
    path("setup/wizard/<str:step_key>/", wizard_views.wizard_step, name="wizard_step"),
    path("setup/wizard/reset/", wizard_views.wizard_reset, name="wizard_reset"),
]
