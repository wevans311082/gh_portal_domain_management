from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.urls import reverse

from apps.accounts.models import User
from apps.admin_tools.search import GROUPS, matching_records, _prefix
from apps.admin_tools.search_keys import FullName
from apps.billing.models import Invoice, Quote
from apps.domains.models import Domain
from apps.products.models import Package
from apps.services.models import Service
from apps.support.models import SupportTicket

pytestmark = pytest.mark.django_db


@pytest.fixture
def records():
    customer = User.objects.create_user(email='needle@example.test', first_name='Needle', last_name='Customer', password='secret-password')
    package = Package.objects.create(name='Hosting', slug='search-hosting', price_monthly=10, price_annually=100)
    domain = Domain.objects.create(user=customer, name='needle.example', tld='example', epp_code='NEVER-DOMAIN-SECRET')
    service = Service.objects.create(user=customer, package=package, domain_name='needle.host', provisioning_config={'password': 'NEVER-SERVICE-SECRET'})
    invoice = Invoice.objects.create(user=customer, number='NEEDLE-I-001', total=Decimal('999.11'))
    quote = Quote.objects.create(user=customer, number='NEEDLE-Q-001', internal_notes='NEVER-INTERNAL-NOTES')
    ticket = SupportTicket.objects.create(user=customer, subject='Needle support subject')
    return dict(zip(('customers', 'domains', 'services', 'invoices', 'quotes', 'tickets'), (customer, domain, service, invoice, quote, ticket)))


@pytest.fixture
def actor(client):
    actor = User.objects.create_user(email='search-staff@example.test', password='staff-password', is_staff=True)
    actor.groups.add(Group.objects.get(name='Portal Auditor'))
    client.force_login(actor)
    return actor


def search_url():
    return reverse('admin_tools:search')


def test_grouped_results_have_direct_authorized_read_only_links(client, actor, records):
    response = client.get(search_url(), {'q': 'needle'})
    assert response.status_code == 200
    assert {section['key'] for section in response.context['sections']} == set(records)
    for key, record in records.items():
        section = next(section for section in response.context['sections'] if section['key'] == key)
        assert section['count'] == 1
        expected = reverse('admin_tools:search_record', args=[key, record.pk])
        assert section['items'][0]['url'] == expected
        detail = client.get(expected)
        assert detail.status_code == 200 and b'Manage this record' not in detail.content
        assert b'NEVER-' not in detail.content
    assert b'NEVER-' not in response.content
    assert response['Cache-Control'] == 'no-store'


@pytest.mark.parametrize('role,visible', [
    ('Portal Support', {'customers', 'domains', 'services', 'tickets'}),
    ('Portal Billing', {'customers', 'invoices', 'quotes'}),
    ('Portal Domain Operations', {'customers', 'domains'}),
    ('Portal Hosting Operations', {'customers', 'services'}),
    ('Portal Auditor', {'customers', 'domains', 'services', 'invoices', 'quotes', 'tickets'}),
])
def test_roles_filter_counts_snippets_suggestions_and_record_access(client, actor, records, role, visible):
    actor.groups.set([Group.objects.get(name=role)])
    response = client.get(search_url(), {'q': 'needle'})
    assert {section['key'] for section in response.context['sections']} == visible
    suggestions = client.get(reverse('admin_tools:search_suggestions'), {'q': 'needle'})
    assert suggestions.status_code == 200 and suggestions['Cache-Control'] == 'no-store'
    labels = ' '.join(item['label'] for item in suggestions.json()['suggestions'])
    for group in GROUPS:
        record = records[group.key]
        if group.key not in visible:
            assert group.label + ':' not in labels
            assert client.get(search_url(), {'q': 'needle', 'group': group.key}).status_code == 404
            assert client.get(reverse('admin_tools:search_record', args=[group.key, record.pk])).status_code == 404


def test_billing_information_not_queried_or_returned_to_support(client, actor, records):
    actor.groups.set([Group.objects.get(name='Portal Support')])
    from django.test.utils import CaptureQueriesContext
    with CaptureQueriesContext(connection) as captured:
        response = client.get(search_url(), {'q': 'NEEDLE-I'})
    assert b'999.11' not in response.content and b'NEEDLE-I-001' not in response.content
    assert not any('billing_invoice' in query['sql'] or 'billing_quote' in query['sql'] for query in captured.captured_queries)


def test_permission_revocation_takes_effect_on_next_html_json_and_detail_request(client, actor, records):
    assert client.get(search_url(), {'q': 'needle'}).status_code == 200
    actor.groups.clear()
    assert client.get(search_url(), {'q': 'needle'}).status_code == 403
    assert client.get(reverse('admin_tools:search_suggestions'), {'q': 'needle'}).status_code == 403
    assert client.get(reverse('admin_tools:search_record', args=['invoices', records['invoices'].pk])).status_code == 403


@pytest.mark.parametrize('query,key', [('NEEDLE@', 'customers'), ('needle cust', 'customers'), ('needle.ex', 'domains'), ('NEEDLE.H', 'services'), ('needle-i-001', 'invoices'), ('needle-q', 'quotes')])
def test_exact_and_prefix_matches_are_case_insensitive(records, actor, query, key):
    group = next(group for group in GROUPS if group.key == key)
    assert records[key].pk in matching_records(group, query, actor).values_list('pk', flat=True)


def test_ticket_reference_prefix_uses_numeric_ids_and_handles_large_input(client, actor, records):
    ticket = records['tickets']
    ticket.pk = 12345
    ticket.save(force_insert=True)
    response = client.get(search_url(), {'q': '#123', 'group': 'tickets'})
    assert response.context['sections'][0]['items'][0]['title'] == '#12345'
    assert client.get(search_url(), {'q': '#' + '9' * 100, 'group': 'tickets'}).status_code == 200


def test_literal_wildcards_do_not_broaden_search(client, actor, records):
    for query in ('%%', '__', "' OR 1=1 --"):
        response = client.get(search_url(), {'q': query})
        assert all(section['count'] == 0 for section in response.context['sections'])


def test_staff_and_owner_accounts_are_not_customer_search_records(client, actor, records):
    owner = User.objects.create_user(email='needle-owner@example.test', password='owner-password', is_staff=True, is_superuser=True)
    response = client.get(search_url(), {'q': 'needle', 'group': 'customers'})
    assert response.context['sections'][0]['count'] == 1
    assert client.get(reverse('admin_tools:search_record', args=['customers', owner.pk])).status_code == 404


def test_group_pagination_is_stable_and_results_are_capped(client, actor, records, monkeypatch):
    from apps.admin_tools import search
    monkeypatch.setattr(search, 'MAX_RESULTS', 25)
    for index in range(30):
        Invoice.objects.create(user=records['customers'], number=f'NEEDLE-I-{index + 100}')
    first = client.get(search_url(), {'q': 'needle-i', 'group': 'invoices'})
    second = client.get(search_url(), {'q': 'needle-i', 'group': 'invoices', 'page': '2'})
    section = first.context['sections'][0]
    assert section['count'] == 25 and section['truncated']
    assert len(section['items']) == 20 and len(second.context['sections'][0]['items']) == 5
    assert {item['url'] for item in section['items']}.isdisjoint(item['url'] for item in second.context['sections'][0]['items'])
    far = client.get(search_url(), {'q': 'needle-i', 'group': 'invoices', 'page': '999999'})
    assert far.context['page_obj'].number == 2


@pytest.mark.parametrize('query', ['x', 'x' * 256, 'ab\ncd'])
def test_invalid_search_does_not_query_records(client, actor, query):
    response = client.get(search_url(), {'q': query})
    assert response.status_code == 200 and response.context['form'].errors
    assert not response.context['sections']
    assert client.get(reverse('admin_tools:search_suggestions'), {'q': query}).status_code == 400


def test_index_expressions_are_used_by_sqlite_for_email_domain_and_full_name(records, actor):
    if connection.vendor != 'sqlite':
        pytest.skip('SQLite query plan assertion; PostgreSQL runtime is a separate verification scope')
    for queryset, index in [
        (_prefix(User.objects.all(), 'email', 'needle'), 'search_user_email'),
        (_prefix(User.objects.all(), FullName('first_name', 'last_name'), 'needle cust'), 'search_user_full_name'),
        (_prefix(Domain.objects.all(), 'name', 'needle'), 'search_domain_name'),
    ]:
        assert index in queryset.explain()
    assert 'search_domain_name' in matching_records(GROUPS[1], 'needle', actor).explain()


def test_updates_and_deletes_are_reflected_without_a_stale_search_index(client, actor, records):
    domain = records['domains']
    Domain.objects.filter(pk=domain.pk).update(name='changed.example')
    assert not matching_records(GROUPS[1], 'needle.ex', actor).exists()
    assert matching_records(GROUPS[1], 'changed.ex', actor).get().pk == domain.pk
    detail = reverse('admin_tools:search_record', args=['domains', domain.pk])
    domain.delete()
    assert client.get(detail).status_code == 404


def test_result_snippets_escape_markup_and_suggestions_are_data_only(client, actor, records):
    records['customers'].first_name = 'needle<script>alert(1)</script>'
    records['customers'].save(update_fields=['first_name'])
    response = client.get(search_url(), {'q': 'needle', 'group': 'customers'})
    assert b'&lt;script&gt;' in response.content and b'needle<script>' not in response.content
    suggestions = client.get(reverse('admin_tools:search_suggestions'), {'q': 'needle'})
    assert suggestions['Content-Type'].startswith('application/json')
    assert all(set(item) == {'value', 'label'} for item in suggestions.json()['suggestions'])


def test_anonymous_nonstaff_and_content_only_actors_cannot_search(client, records):
    assert client.get(search_url(), {'q': 'needle'}).status_code == 302
    client.force_login(records['customers'])
    assert client.get(search_url(), {'q': 'needle'}).status_code == 302
    staff = User.objects.create_user(email='content-search@example.test', password='password', is_staff=True)
    staff.groups.add(Group.objects.get(name='Portal Content'))
    client.force_login(staff)
    assert client.get(search_url(), {'q': 'needle'}).status_code == 403
    assert b'aria-label="Search admin records"' not in client.get(reverse('admin_tools:dashboard')).content


def test_suggestions_are_bounded_and_unknown_groups_do_not_disclose_records(client, actor, records):
    for index in range(10):
        User.objects.create_user(email=f'needle-{index}@example.test', password='test-password')
    suggestions = client.get(reverse('admin_tools:search_suggestions'), {'q': 'needle'}).json()['suggestions']
    assert len(suggestions) <= 12
    assert sum(item['label'].startswith('Customers:') for item in suggestions) == 2
    assert client.get(search_url(), {'q': 'needle', 'group': 'secret-records'}).status_code == 404
    assert client.get(reverse('admin_tools:search_suggestions'), {'q': 'needle', 'group': 'secret-records'}).status_code == 404


def test_non_ascii_customer_names_match_exact_and_prefix_cases(client, actor):
    customer = User.objects.create_user(email='unicode@example.test', first_name='Émile', last_name='Müller', password='password')
    for query in ('Émile', 'ém', 'MÜL', 'Émile Müll'):
        response = client.get(search_url(), {'q': query, 'group': 'customers'})
        assert response.context['sections'][0]['items'][0]['title'] == customer.email


def test_related_customer_name_cannot_be_probed_without_customer_permission(client, actor, records):
    from django.contrib.auth.models import Permission
    actor.groups.clear()
    actor.user_permissions.add(Permission.objects.get(content_type__app_label='admin_tools', codename='view_domains'))
    customer = records['customers']
    customer.first_name = 'HiddenCustomerName'
    customer.save(update_fields=['first_name'])
    response = client.get(search_url(), {'q': 'HiddenCustomer'})
    assert [section['key'] for section in response.context['sections']] == ['domains']
    assert response.context['sections'][0]['count'] == 0
    assert client.get(reverse('admin_tools:search_suggestions'), {'q': 'HiddenCustomer'}).json()['suggestions'] == []
    actor.user_permissions.add(Permission.objects.get(content_type__app_label='admin_tools', codename='view_customers'))
    response = client.get(search_url(), {'q': 'HiddenCustomer', 'group': 'domains'})
    assert response.context['sections'][0]['count'] == 1


def test_search_and_suggestions_share_request_budget(client, actor, monkeypatch):
    from apps.core.abuse import POLICIES, PublicPolicy
    policy = PublicPolicy(frozenset({'GET'}), 0, 2, 60, 1000)
    for route in ('admin_tools:search', 'admin_tools:search_suggestions'):
        monkeypatch.setitem(POLICIES, route, policy)
    assert client.get(search_url()).status_code == 200
    assert client.get(reverse('admin_tools:search_suggestions')).status_code == 200
    response = client.get(search_url(), {'q': 'needle'})
    assert response.status_code == 429 and int(response['Retry-After']) > 0


def test_search_fails_closed_when_shared_counter_is_unavailable(client, actor, monkeypatch):
    from django.core.cache import caches
    from django.conf import settings
    def unavailable(*args, **kwargs):
        raise RuntimeError('private-backend-credentials')
    monkeypatch.setattr(caches[settings.PUBLIC_ABUSE_CACHE_ALIAS], 'incr', unavailable)
    response = client.get(reverse('admin_tools:search_suggestions'), {'q': 'needle'})
    assert response.status_code == 503 and b'private-backend' not in response.content


def test_search_policy_capabilities_match_group_inventory():
    from apps.admin_tools.access import ROUTE_POLICIES
    expected = {group.capability for group in GROUPS}
    for name in ('search', 'search_suggestions', 'search_record'):
        assert set(ROUTE_POLICIES[name].read_any) == expected


def test_manage_links_follow_separate_route_permissions(client, actor, records):
    actor.groups.set([Group.objects.get(name='Portal Billing')])
    assert b'Manage this record' in client.get(reverse('admin_tools:search_record', args=['invoices', records['invoices'].pk])).content
    assert b'Manage this record' not in client.get(reverse('admin_tools:search_record', args=['customers', records['customers'].pk])).content
    assert client.post(search_url(), {'q': 'needle'}).status_code == 403


def test_postgresql_prefix_sql_uses_index_collation_without_opening_a_server(monkeypatch):
    from django.db.backends.postgresql.base import DatabaseWrapper
    pg = DatabaseWrapper({'NAME': 'compile_only'}, alias='search_compile_only')
    def forbid_connection():
        raise AssertionError('SQL compilation must not contact a database server')
    monkeypatch.setattr(pg, 'ensure_connection', forbid_connection)
    queryset = _prefix(User.objects.all(), FullName('first_name', 'last_name'), 'needle')
    sql, params = queryset.query.get_compiler(connection=pg).as_sql()
    assert 'COLLATE "C"' in sql and 'LOWER(' in sql
    assert " || ' ' || " in sql
    assert 'needle' not in sql and 'needle' in params
