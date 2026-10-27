from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from .access import has_route_access
from .decorators import staff_member_required
from .search import (SearchForm, permitted_groups, bounded_page, result_item, record_details,
                     PREVIEW_SIZE, PAGE_SIZE, MAX_RESULTS, normalize_query)


def search_context(request):
    groups = permitted_groups(request.user)
    selected = request.GET.get('group', '')
    if selected and selected not in {group.key for group in groups}:
        raise Http404
    form = SearchForm(request.GET, groups=groups)
    return groups, selected, form


@staff_member_required
@require_GET
def search(request):
    groups, selected, form = search_context(request)
    sections = []
    page = None
    if form.is_valid() and form.cleaned_data['q']:
        query = normalize_query(form.cleaned_data['q'])
        for group in groups:
            if selected and group.key != selected:
                continue
            current, truncated = bounded_page(group, query, request.user, request.GET.get('page', '1') if selected else 1,
                                              PAGE_SIZE if selected else PREVIEW_SIZE)
            sections.append({'key': group.key, 'label': group.label, 'count': current.paginator.count,
                             'truncated': truncated, 'items': [result_item(group, record, request.user) for record in current]})
            if selected:
                page = current
    return render(request, 'admin_tools/search.html', {'form': form, 'search_groups': groups, 'sections': sections,
                                                     'selected_group': selected, 'page_obj': page, 'max_results': MAX_RESULTS})


@staff_member_required
@require_GET
def search_suggestions(request):
    groups, selected, form = search_context(request)
    if not form.is_valid():
        return JsonResponse({'error': 'Enter between 2 and 255 characters of valid search text.', 'suggestions': []}, status=400)
    query = form.cleaned_data['q']
    suggestions = []
    if query:
        query = normalize_query(query)
        from .search import matching_records
        for group in groups:
            if selected and group.key != selected:
                continue
            for record in matching_records(group, query, request.user)[:2]:
                item = result_item(group, record, request.user)
                suggestions.append({'value': item['title'], 'label': f"{group.label}: {item['title']}"})
    return JsonResponse({'suggestions': suggestions})


@staff_member_required
@require_GET
def search_record(request, kind, pk):
    group = next((group for group in permitted_groups(request.user) if group.key == kind), None)
    if group is None:
        raise Http404
    queryset = group.model.objects.all()
    if kind == 'customers':
        queryset = queryset.filter(is_staff=False, is_superuser=False).only('pk', 'email', 'first_name', 'last_name', 'is_active')
    else:
        display_fields = {
            'domains': ('name', 'expires_at', 'auto_renew'),
            'services': ('domain_name', 'cpanel_domain', 'next_due_date'),
            'tickets': ('subject', 'priority'),
            'invoices': ('number', 'currency', 'total'),
            'quotes': ('number', 'currency', 'total', 'lead_email'),
        }
        queryset = queryset.select_related('user').only('pk', 'status', 'user__email', *display_fields[kind])
    record = get_object_or_404(queryset, pk=pk)
    item = result_item(group, record, request.user)
    return render(request, 'admin_tools/search_record.html', {
        'item': item, 'details': record_details(group, record),
        'record_heading': {'customers': 'Customer account', 'domains': 'Domain details', 'services': 'Hosting service',
                           'invoices': 'Invoice details', 'quotes': 'Quote details', 'tickets': 'Support ticket'}[kind],
        'manage_route': group.manage_route if has_route_access(request.user, group.manage_route) else '', 'record_pk': record.pk,
    })
