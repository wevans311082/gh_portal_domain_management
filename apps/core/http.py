"""HTTP helpers shared across middleware and views."""


def get_client_ip(request) -> str:
    """Return the connecting client IP.

    When ``X-Forwarded-For`` is present, use the *last* hop. Nginx appends
    ``$remote_addr`` via ``$proxy_add_x_forwarded_for``, so the last value is
    the socket peer of the trusted proxy. The first hop is attacker-controlled.
    """
    forwarded_for = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    if forwarded_for:
        hops = [part.strip() for part in forwarded_for.split(",") if part.strip()]
        if hops:
            return hops[-1]
    return (request.META.get("REMOTE_ADDR") or "").strip()
