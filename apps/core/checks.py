from django.core.checks import Error, Tags, register

from .http import trusted_proxy_networks


@register(Tags.security)
def check_trusted_proxies(app_configs, **kwargs):
    try:
        trusted_proxy_networks()
    except (ValueError, TypeError) as error:
        return [Error(str(error), id="core.E001", hint="Set TRUSTED_PROXY_CIDRS to explicit socket-peer addresses or canonical CIDRs.")]
    return []


@register(Tags.security, deploy=True)
def check_public_abuse_cache(app_configs, **kwargs):
    from django.conf import settings
    alias = settings.PUBLIC_ABUSE_CACHE_ALIAS
    configuration = settings.CACHES.get(alias, {})
    # Redis is the project's configured shared store and supplies atomic incr.
    if configuration.get("BACKEND") not in {"django_redis.cache.RedisCache", "django.core.cache.backends.redis.RedisCache"}:
        return [Error("Public abuse controls require a shared Redis cache with atomic counters.",
                      id="core.E002", hint=f"Configure cache alias {alias}; do not use per-process or dummy caches.")]
    return []


@register(Tags.security, deploy=True)
def check_public_delivery_queue(app_configs, **kwargs):
    from django.conf import settings
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        return [Error("Public delivery must run asynchronously outside HTTP requests.", id="core.E003",
                      hint="Do not enable CELERY_TASK_ALWAYS_EAGER for the running application.")]
    return []
