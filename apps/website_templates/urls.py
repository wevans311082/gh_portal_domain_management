from django.urls import path

from . import views

app_name = "website_templates"

urlpatterns = [
    path("", views.gallery, name="gallery"),
    path("my/", views.my_templates, name="my_templates"),
    path("thumbnail/<slug:slug>/", views.thumbnail, name="thumbnail"),
    path("preview/<slug:slug>/", views.preview, name="preview"),
    # Serves any static file from the extracted template (index.html, css/, js/, images/ …)
    path("preview/<slug:slug>/files/<path:file_path>", views.preview_file, name="preview_file"),
    path("install/<slug:slug>/", views.install_confirm, name="install_confirm"),
    path("install/<slug:slug>/confirm/", views.install, name="install"),
    path("uninstall/<int:installation_id>/", views.uninstall, name="uninstall"),
]
