"""Small, escaped template components; permissions stay in views/templates."""
from django import template

register = template.Library()


@register.inclusion_tag("admin_tools/components/pagination.html", takes_context=True)
def admin_pagination(context, page_obj, label="Results pages"):
    request = context.get("request")
    params = request.GET.copy() if request else None
    def page_url(number):
        if params is None:
            return f"?page={number}"
        query = params.copy()
        query.setlist("page", [str(number)])
        return "?" + query.urlencode()
    return {
        "page_obj": page_obj, "label": label,
        "previous_url": page_url(page_obj.previous_page_number()) if page_obj.has_previous() else "",
        "next_url": page_url(page_obj.next_page_number()) if page_obj.has_next() else "",
    }
