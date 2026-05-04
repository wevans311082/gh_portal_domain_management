from django.urls import path

from apps.billing import public_views

app_name = "billing_public"

urlpatterns = [
    path("", public_views.quote_builder, name="quote_builder"),
    path("submit/", public_views.quote_submit, name="quote_submit"),
    path("thanks/", public_views.quote_thanks, name="quote_thanks"),
    path("q/<uuid:token>/", public_views.quote_public, name="quote_public"),
    path("q/<uuid:token>/pdf/", public_views.quote_public_pdf, name="quote_public_pdf"),
    path("q/<uuid:token>/accept/", public_views.quote_public_accept, name="quote_public_accept"),
    path(
        "q/<uuid:token>/finalize/",
        public_views.quote_public_accept_continue,
        name="quote_public_accept_continue",
    ),
]
