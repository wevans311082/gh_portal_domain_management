from django import template

register = template.Library()


@register.filter
def currency(value):
    try:
        return f"£{float(value):.2f}"
    except (ValueError, TypeError):
        return value
