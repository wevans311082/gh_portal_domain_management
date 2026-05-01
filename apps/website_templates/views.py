import mimetypes
import os
import pathlib

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import TemplateInstallation, WebsiteTemplate

# Root where extracted templates live on disk
EXTRACTED_ROOT = pathlib.Path(
    getattr(settings, "WEBSITE_TEMPLATES_EXTRACTED_ROOT", "website_templates/extracted")
)


# ---------------------------------------------------------------------------
# Gallery
# ---------------------------------------------------------------------------

def gallery(request):
    """Public-facing (authenticated) template gallery."""
    qs = WebsiteTemplate.objects.filter(is_active=True)

    category_filter = request.GET.get("category", "")
    search_q = request.GET.get("q", "").strip()

    if category_filter:
        qs = qs.filter(category=category_filter)
    if search_q:
        from django.db.models import Q
        qs = qs.filter(
            Q(name__icontains=search_q) |
            Q(description__icontains=search_q) |
            Q(category__icontains=search_q)
        )

    categories = WebsiteTemplate.CATEGORY_CHOICES

    # My active installations
    if request.user.is_authenticated:
        my_installs = TemplateInstallation.objects.filter(
            user=request.user, status="active"
        ).values_list("template_id", flat=True)
    else:
        my_installs = []

    return render(
        request,
        "website_templates/gallery.html",
        {
            "templates": qs,
            "categories": categories,
            "category_filter": category_filter,
            "search_q": search_q,
            "my_installs": list(my_installs),
            "total_in_db": WebsiteTemplate.objects.filter(is_active=True).count(),
        },
    )


# ---------------------------------------------------------------------------
# Preview — serve raw static file from the extracted folder
# ---------------------------------------------------------------------------

def _safe_resolved_path(slug: str, sub_path: str) -> pathlib.Path | None:
    """
    Resolve a sub-path inside the template's extracted directory.
    Returns None if the resolved path escapes the root (path traversal guard).
    """
    template_dir = EXTRACTED_ROOT / slug
    try:
        resolved = (template_dir / sub_path).resolve()
        template_dir_resolved = template_dir.resolve()
        resolved.relative_to(template_dir_resolved)  # raises ValueError if escaping
        return resolved
    except (ValueError, Exception):
        return None


def preview(request, slug):
    """Serve index.html from the extracted template directory inside an iframe wrapper."""
    template = get_object_or_404(WebsiteTemplate, slug=slug, is_active=True)
    return render(request, "website_templates/preview.html", {"template": template})


def preview_file(request, slug, file_path="index.html"):
    """
    Serve any static file from the extracted template folder.
    Security: path traversal is prevented by _safe_resolved_path().
    """
    template_obj = get_object_or_404(WebsiteTemplate, slug=slug, is_active=True)

    resolved = _safe_resolved_path(slug, file_path)
    if resolved is None or not resolved.is_file():
        raise Http404("File not found")

    mime, _ = mimetypes.guess_type(str(resolved))
    mime = mime or "application/octet-stream"

    # Security: force plain text for HTML so scripts don't run in same-origin
    # context unless the request is from our preview iframe wrapper.
    referer = request.META.get("HTTP_REFERER", "")
    our_origin = request.build_absolute_uri("/")
    if mime == "text/html" and not referer.startswith(our_origin):
        raise Http404("Direct HTML access not allowed")

    def _iter_file(path, chunk=64 * 1024):
        with open(path, "rb") as fh:
            while True:
                data = fh.read(chunk)
                if not data:
                    break
                yield data

    response = StreamingHttpResponse(_iter_file(resolved), content_type=mime)
    # Prevent the template content from navigating the top frame
    response["X-Frame-Options"] = "SAMEORIGIN"
    response["Content-Security-Policy"] = "sandbox allow-scripts allow-same-origin"
    return response


def thumbnail(request, slug):
    """
    Serve a generated or static thumbnail image for a template.
    Falls back to a generated SVG placeholder when no screenshot exists.
    """
    template_obj = get_object_or_404(WebsiteTemplate, slug=slug)
    thumb_path = EXTRACTED_ROOT / slug / "_thumbnail.png"
    if thumb_path.is_file():
        with open(thumb_path, "rb") as fh:
            return HttpResponse(fh.read(), content_type="image/png")

    # Return an SVG placeholder
    initials = template_obj.name[:2].upper()
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="400" height="250">
  <rect width="400" height="250" fill="#0369a1"/>
  <text x="200" y="140" font-family="sans-serif" font-size="64" fill="white"
        text-anchor="middle" dominant-baseline="middle">{initials}</text>
  <text x="200" y="200" font-family="sans-serif" font-size="16" fill="#bae6fd"
        text-anchor="middle">{template_obj.name}</text>
</svg>"""
    return HttpResponse(svg.encode(), content_type="image/svg+xml")


# ---------------------------------------------------------------------------
# Install / Uninstall
# ---------------------------------------------------------------------------

@login_required
def install_confirm(request, slug):
    """Show install confirmation page."""
    template_obj = get_object_or_404(WebsiteTemplate, slug=slug, is_active=True)

    # Services the user has (for domain association)
    from apps.services.models import Service
    user_services = Service.objects.filter(
        user=request.user, status="active"
    ).select_related("package")

    return render(
        request,
        "website_templates/install_confirm.html",
        {"template": template_obj, "user_services": user_services},
    )


@login_required
@require_POST
def install(request, slug):
    """Record a template installation."""
    template_obj = get_object_or_404(WebsiteTemplate, slug=slug, is_active=True)
    service_domain = request.POST.get("service_domain", "").strip()[:253]

    # If already installed and active, skip
    existing = TemplateInstallation.objects.filter(
        user=request.user, template=template_obj, status="active"
    ).first()
    if not existing:
        TemplateInstallation.objects.create(
            user=request.user,
            template=template_obj,
            service_domain=service_domain,
        )
        messages.success(
            request,
            f'"{template_obj.name}" has been added to your account. '
            "Download the files from the preview page.",
        )
    else:
        messages.info(request, f'"{template_obj.name}" is already in your installations.')

    return redirect("website_templates:gallery")


@login_required
@require_POST
def uninstall(request, installation_id):
    """Mark an installation as removed."""
    install_obj = get_object_or_404(
        TemplateInstallation, pk=installation_id, user=request.user, status="active"
    )
    install_obj.status = "removed"
    install_obj.removed_at = timezone.now()
    install_obj.save(update_fields=["status", "removed_at"])
    messages.success(request, "Template removed from your installations.")
    return redirect("website_templates:my_templates")


@login_required
def my_templates(request):
    """Show all templates the authenticated user has installed."""
    installs = TemplateInstallation.objects.filter(
        user=request.user, status="active"
    ).select_related("template")
    return render(request, "website_templates/my_templates.html", {"installs": installs})
