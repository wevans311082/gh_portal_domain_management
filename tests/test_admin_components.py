from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit
from pathlib import Path
import re

import pytest
from django import forms
from django.core.paginator import Paginator
from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import RequestFactory


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def test_pagination_encodes_filters_preserves_multi_values_and_replaces_page():
    request = RequestFactory().get('/admin-tools/users/', {'q': 'A&B <script> /?=', 'status': ['active', 'pending'], 'page': ['99', '100']})
    page = Paginator(list(range(30)), 10).page(2)
    html = Template('{% load admin_components %}{% admin_pagination page_obj %}').render(Context({'request': request, 'page_obj': page}))
    links = [attrs for tag, attrs in Elements(html).elements if tag == 'a']
    assert {attrs['rel'] for attrs in links} == {'prev', 'next'}
    for attrs in links:
        query = parse_qs(urlsplit(attrs['href']).query)
        assert query['q'] == ['A&B <script> /?=']
        assert query['status'] == ['active', 'pending']
        assert query['page'] == ['1' if attrs['rel'] == 'prev' else '3']
    assert '<script>' not in html


def test_single_page_does_not_render_misleading_navigation():
    page = Paginator([], 10).page(1)
    html = Template('{% load admin_components %}{% admin_pagination page_obj %}').render(Context({'page_obj': page}))
    assert '<nav' not in html


def test_components_escape_labels_and_restrict_status_classes():
    html = render_to_string('admin_tools/components/status.html', {'label': '<script>unsafe</script>', 'tone': 'danger" onclick="unsafe'})
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert 'onclick=' not in html and 'admin-status-neutral' in html
    heading = render_to_string('admin_tools/components/page_header.html', {'heading': '<img src=x onerror=alert(1)>'})
    assert '<img' not in heading and '&lt;img' in heading


@pytest.mark.parametrize('field_name', ['name', 'choices'])
def test_shared_fields_associate_prefixed_errors_help_and_summary(field_name):
    class ExampleForm(forms.Form):
        name = forms.CharField(help_text='Enter your name')
        choices = forms.MultipleChoiceField(choices=[('a', 'Choice A')], widget=forms.CheckboxSelectMultiple, help_text='Select a choice')
    form = ExampleForm({}, prefix='example')
    assert not form.is_valid()
    field = form[field_name]
    html = render_to_string('admin_tools/components/field.html', {'field': field})
    elements = Elements(html).elements
    ids = {attrs['id'] for _, attrs in elements if 'id' in attrs}
    assert field.auto_id + '_error' in ids and field.auto_id + '_helptext' in ids
    for _, attrs in elements:
        for reference in attrs.get('aria-describedby', '').split():
            assert reference in ids
    summary = render_to_string('admin_tools/components/error_summary.html', {'form': form})
    target = field.auto_id + '_group' if field.use_fieldset else field.id_for_label
    assert f'href="#{target}"' in summary and target in ids


def test_hidden_field_errors_do_not_link_to_unfocusable_controls():
    class ExampleForm(forms.Form):
        token = forms.CharField(widget=forms.HiddenInput)
    form = ExampleForm({})
    assert not form.is_valid()
    html = render_to_string('admin_tools/components/error_summary.html', {'form': form})
    assert '<a ' not in html and 'This field is required.' in html


def test_semantic_text_tokens_have_normal_text_contrast():
    css = (Path(__file__).resolve().parents[1] / 'static/css/admin.css').read_text(encoding='utf-8')
    tokens = dict(re.findall(r'--([\w-]+):\s*(#[0-9a-fA-F]{6})', css))
    def luminance(colour):
        values = [int(colour[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in values]
        return sum(weight * value for weight, value in zip((.2126, .7152, .0722), linear))
    pairs = [('admin-ink', 'admin-surface'), ('admin-muted', 'admin-surface'), ('admin-ink', 'admin-canvas')]
    pairs += [(f'admin-{tone}-ink', f'admin-{tone}-surface') for tone in ('success', 'warning', 'danger', 'info')]
    for foreground, background in pairs:
        light, dark = sorted((luminance(tokens[foreground]), luminance(tokens[background])), reverse=True)
        assert (light + .05) / (dark + .05) >= 4.5, (foreground, background)
    assert (luminance('#ffffff') + .05) / (luminance(tokens['admin-accent']) + .05) >= 4.5
