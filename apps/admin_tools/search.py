"""Permission-scoped, bounded global search over live source records."""
from dataclasses import dataclass

from django import forms
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Q
from django.urls import reverse

from apps.billing.models import Invoice, Quote
from apps.domains.models import Domain
from apps.services.models import Service
from apps.support.models import SupportTicket
from .access import is_owner
from .search_keys import SearchKey, FullName

MAX_RESULTS = 500
PAGE_SIZE = 20
PREVIEW_SIZE = 5


@dataclass(frozen=True)
class SearchText:
    value: str


def normalize_query(query):
    # Match the same case conversion used by the indexed source expression.
    # This is one parameterized scalar query, never a scan of customer data.
    function = 'PORTAL_LOWER' if connection.vendor == 'sqlite' else 'LOWER'
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT {function}(%s)', [query])
        return SearchText(cursor.fetchone()[0])


@dataclass(frozen=True)
class SearchGroup:
    key: str
    label: str
    model: type
    capability: str
    fields: tuple
    manage_route: str


GROUPS = (
    SearchGroup('customers', 'Customers', get_user_model(), 'view_customers', ('email', 'first_name', 'last_name'), 'user_edit'),
    SearchGroup('domains', 'Domains', Domain, 'view_domains', ('name',), 'domains_edit'),
    SearchGroup('services', 'Hosting services', Service, 'view_services', ('domain_name', 'cpanel_domain'), 'services_edit'),
    SearchGroup('invoices', 'Invoices', Invoice, 'view_billing', ('number',), 'invoice_edit'),
    SearchGroup('quotes', 'Quotes', Quote, 'view_billing', ('number', 'lead_email', 'lead_name'), 'quote_edit'),
    SearchGroup('tickets', 'Support tickets', SupportTicket, 'view_support', (), 'tickets_edit'),
)


def permitted_groups(user):
    return [group for group in GROUPS if is_owner(user) or user.has_perm('admin_tools.' + group.capability)]


class SearchForm(forms.Form):
    q = forms.CharField(label='Search records', min_length=2, max_length=255, required=False,
                        widget=forms.TextInput(attrs={'type': 'search', 'autocomplete': 'off', 'list': 'search-suggestions'}),
                        help_text='Enter an email, name, domain, document number or ticket reference. Matches start with your text; use #123 for a ticket.')
    group = forms.ChoiceField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, groups=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['group'].choices = [('', 'All available groups')] + [(group.key, group.label) for group in groups]

    def clean_q(self):
        value = self.cleaned_data['q'].strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise forms.ValidationError('Enter search text without control characters.')
        return value


def _prefix(queryset, field, term):
    # Binary/C collation gives a predictable index range. LIKE also verifies the
    # prefix, with Django escaping literal wildcard characters in user input.
    upper = None
    for index in range(len(term) - 1, -1, -1):
        ordinal = ord(term[index])
        if ordinal < 0x10ffff:
            ordinal += 1
            if 0xd800 <= ordinal <= 0xdfff:
                ordinal = 0xe000
            upper = term[:index] + chr(ordinal)
            break
    result = queryset.alias(_search_key=SearchKey(field)).filter(_search_key__gte=term, _search_key__startswith=term)
    return result.filter(_search_key__lt=upper) if upper else result


def customer_matches(term):
    User = get_user_model()
    base = User.objects.filter(is_staff=False, is_superuser=False)
    matches = [_prefix(base, field, term).order_by().values('pk') for field in ('email', 'first_name', 'last_name')]
    # A full-name prefix has its own matching expression/index.
    matches.append(_prefix(base, FullName('first_name', 'last_name'), term).order_by().values('pk'))
    return matches[0].union(*matches[1:])


def matching_records(group, query, actor):
    if not actor.is_authenticated or not actor.is_active or not actor.is_staff:
        return group.model.objects.none()
    if not is_owner(actor) and not actor.has_perm('admin_tools.' + group.capability):
        return group.model.objects.none()
    term = query.value if isinstance(query, SearchText) else normalize_query(query).value
    base = group.model.objects.all()
    if group.key == 'customers':
        return base.filter(pk__in=customer_matches(term)).order_by('email', 'pk')
    candidates = [_prefix(base, field, term).order_by().values('pk') for field in group.fields]
    if is_owner(actor) or actor.has_perm('admin_tools.view_customers'):
        candidates.append(base.filter(user_id__in=customer_matches(term)).order_by().values('pk'))
    if group.key == 'tickets':
        reference = term.removeprefix('#')
        if reference.isascii() and reference.isdecimal() and len(reference) <= 19:
            # Ticket references use the existing numeric primary-key index.
            # Prefix ranges avoid casting every id to text or scanning bodies.
            number = int(reference)
            if 0 < number <= 9223372036854775807 and str(number) == reference:
                ranges = Q(pk=number)
                for power in range(1, 20 - len(reference)):
                    lower = number * (10 ** power)
                    upper = min((number + 1) * (10 ** power) - 1, 9223372036854775807)
                    if lower <= upper:
                        ranges |= Q(pk__gte=lower, pk__lte=upper)
                candidates.append(base.filter(ranges).order_by().values('pk'))
    if not candidates:
        return base.none()
    identifiers = candidates[0].union(*candidates[1:])
    # The display order is stable across pages and independent of permissions.
    return base.filter(pk__in=identifiers).order_by('pk')


def bounded_page(group, query, actor, page_number=1, size=PAGE_SIZE):
    from django.core.paginator import Paginator
    # Count and navigation are capped, including the SQL count subquery. One
    # extra row communicates truncation without an unbounded total query.
    queryset = matching_records(group, query, actor)[:MAX_RESULTS + 1]
    count = queryset.count()
    paginator = Paginator(queryset[:MAX_RESULTS], size)
    page = paginator.get_page(page_number)
    return page, count > MAX_RESULTS


def result_item(group, record, actor):
    if group.key == 'customers':
        title, summary = record.email, record.full_name
    elif group.key == 'domains':
        title, summary = record.name, record.get_status_display()
    elif group.key == 'services':
        title, summary = record.domain_name or record.cpanel_domain or f'Service #{record.pk}', record.get_status_display()
    elif group.key == 'tickets':
        title, summary = f'#{record.pk}', record.subject
    else:
        title, summary = record.number, record.get_status_display()
    return {'title': title, 'summary': summary, 'group': group.label,
            'url': reverse('admin_tools:search_record', args=[group.key, record.pk])}


def record_details(group, record):
    """Explicit fields only: never serialize models, notes or credentials."""
    if group.key == 'customers':
        return [('Email', record.email), ('Name', record.full_name), ('Status', 'Active' if record.is_active else 'Inactive')]
    fields = [('Customer', record.user.email if record.user_id else record.lead_email), ('Status', record.get_status_display())]
    if group.key == 'domains':
        fields += [('Domain', record.name), ('Expires', record.expires_at), ('Auto-renew', 'On' if record.auto_renew else 'Off')]
    elif group.key == 'services':
        fields += [('Domain', record.domain_name or record.cpanel_domain), ('Next due', record.next_due_date)]
    elif group.key == 'tickets':
        fields += [('Reference', f'#{record.pk}'), ('Subject', record.subject), ('Priority', record.get_priority_display())]
    else:
        fields += [('Number', record.number), ('Total', f'{record.currency} {record.total:.2f}')]
    return fields
