"""Portal identity used in account email, instead of the Django placeholder site."""
from types import SimpleNamespace

from django.conf import settings

_PLACEHOLDER_HOSTS = {
    "",
    "example.com",
    "www.example.com",
    "localhost",
    "testserver",
}
_CANONICAL_FALLBACK = "portal.cyberask.co.uk"


def usable_host(value) -> str:
    """Return a public hostname, or an empty string for placeholders and local hosts."""
    host = (value or "").strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/")[0].split("@")[-1].split(":")[0].rstrip(".")
    if host in _PLACEHOLDER_HOSTS or host.endswith(".example.com"):
        return ""
    if "." not in host or host.replace(".", "").isdigit():
        return ""
    return host


def portal_mail_site(request=None):
    """Name and domain to print in account email."""
    name = (getattr(settings, "SITE_NAME", "") or "").strip() or "Cyber Ask Domains"
    if name.lower() in {"example.com", "example"}:
        name = "Cyber Ask Domains"
    domain = ""
    if request is not None:
        try:
            domain = usable_host(request.get_host())
        except Exception:
            domain = ""
    if not domain:
        domain = usable_host(getattr(settings, "SITE_DOMAIN", ""))
    if not domain:
        domain = _CANONICAL_FALLBACK
    return SimpleNamespace(name=name[:50], domain=domain[:100])


def canonical_portal_site():
    """Stable site row. Request hosts vary; the stored site does not."""
    name = (getattr(settings, "SITE_NAME", "") or "").strip() or "Cyber Ask Domains"
    if name.lower() in {"example.com", "example"}:
        name = "Cyber Ask Domains"
    domain = usable_host(getattr(settings, "SITE_DOMAIN", "")) or _CANONICAL_FALLBACK
    return SimpleNamespace(name=name[:50], domain=domain[:100])


def ensure_portal_site_record():
    """Replace the default example.com site row once settings name a real host."""
    from django.contrib.sites.models import Site

    identity = canonical_portal_site()
    Site.objects.clear_cache()
    try:
        site = Site.objects.get(pk=getattr(settings, "SITE_ID", 1))
    except Site.DoesNotExist:
        return None
    domain_is_placeholder = not usable_host(site.domain)
    name_is_placeholder = (site.name or "").strip().lower() in {"", "example.com", "example"}
    if domain_is_placeholder or name_is_placeholder:
        site.domain = identity.domain
        site.name = identity.name
        site.save(update_fields=["domain", "name"])
    Site.objects.clear_cache()
    return site


def customer_domain_names(user) -> list:
    """Domains and hosting names on the account, without placeholder hosts."""
    if user is None or not getattr(user, "pk", None):
        return []
    found = []
    try:
        from apps.domains.models import Domain

        found.extend(
            Domain.objects.filter(user=user)
            .exclude(status__in=[Domain.STATUS_CANCELLED, Domain.STATUS_TRANSFERRED])
            .order_by("pk").values_list("name", flat=True)[:50]
        )
    except Exception:
        pass
    try:
        from apps.services.models import Service

        services = Service.objects.filter(user=user).exclude(
            status__in=[Service.STATUS_CANCELLED, Service.STATUS_TERMINATED]
        )
        found.extend(services.order_by("pk").values_list("domain_name", flat=True)[:50])
        found.extend(services.order_by("pk").values_list("cpanel_domain", flat=True)[:50])
    except Exception:
        pass

    names = []
    seen = set()
    for raw in found:
        host = usable_host(raw)
        if not host or host in seen:
            continue
        seen.add(host)
        names.append(host)
    names.sort()
    return names
