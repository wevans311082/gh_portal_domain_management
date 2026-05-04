"""Atomic invoice & quote number generation.

Uses ``select_for_update`` against the singleton branding row so concurrent
workers (web + Celery) cannot collide on the same sequence number.
"""
from __future__ import annotations

from datetime import datetime

from django.db import transaction

from apps.billing.models import BillingDocumentBranding


def _format_number(template: str, seq: int) -> str:
    now = datetime.now()
    try:
        return template.format(
            seq=seq,
            yyyy=now.strftime("%Y"),
            yy=now.strftime("%y"),
            mm=now.strftime("%m"),
            dd=now.strftime("%d"),
        )
    except (KeyError, IndexError, ValueError):
        # Fall back to a safe default if the admin saved an invalid template.
        return f"DOC-{now.strftime('%Y')}-{seq:05d}"


def next_invoice_number() -> str:
    with transaction.atomic():
        branding = (
            BillingDocumentBranding.objects.select_for_update()
            .order_by("id")
            .first()
        )
        if branding is None:
            branding = BillingDocumentBranding.objects.create()
            # Re-lock the row we just created so concurrent callers wait.
            branding = (
                BillingDocumentBranding.objects.select_for_update()
                .get(pk=branding.pk)
            )
        branding.invoice_seq = (branding.invoice_seq or 0) + 1
        branding.save(update_fields=["invoice_seq"])
        return _format_number(branding.invoice_number_format, branding.invoice_seq)


def next_quote_number() -> str:
    with transaction.atomic():
        branding = (
            BillingDocumentBranding.objects.select_for_update()
            .order_by("id")
            .first()
        )
        if branding is None:
            branding = BillingDocumentBranding.objects.create()
            branding = (
                BillingDocumentBranding.objects.select_for_update()
                .get(pk=branding.pk)
            )
        branding.quote_seq = (branding.quote_seq or 0) + 1
        branding.save(update_fields=["quote_seq"])
        return _format_number(branding.quote_number_format, branding.quote_seq)
