from django.urls import path
from . import views
from . import wizard_views

app_name = "admin_tools"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("setup/", wizard_views.wizard_index, name="wizard_index"),
    path("setup/<str:step_key>/", wizard_views.wizard_step, name="wizard_step"),
    path("setup/reset/", wizard_views.wizard_reset, name="wizard_reset"),
]
