from django import template

register = template.Library()


@register.filter
def currency(value):
    try:
        return f"£{float(value):.2f}"
    except (ValueError, TypeError):
        return value


@register.filter
def zip_lists(a, b):
    """Zip two lists together: {{ list_a|zip_lists:list_b }}"""
    return zip(a, b)


@register.filter
def list_max(values):
    """Return max of an iterable, 0 if empty: {{ values|list_max }}"""
    try:
        return max(values) if values else 0
    except (TypeError, ValueError):
        return 0


@register.filter
def pct_of(value, total):
    """Return value as percentage of total (0-100): {{ value|pct_of:total }}"""
    try:
        if not total:
            return 0
        return round((float(value) / float(total)) * 100)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0
