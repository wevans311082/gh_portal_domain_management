"""Atomic customer quote acceptance with durable, bounded invoice delivery."""
from django import forms
from django.core.exceptions import PermissionDenied
from django.db import OperationalError, transaction
from django.http import Http404
from django.utils import timezone

from apps.core.http import get_client_ip
from apps.core.public_submissions import accept_submission, SubmissionUnavailable
from .models import Quote, Invoice
from .services import convert_quote_to_invoice


def accept_public_quote(request, token):
    if not request.user.is_authenticated or not request.user.is_active:
        raise PermissionDenied
    try:
        with transaction.atomic():
            quote = Quote.objects.select_for_update().filter(public_token=token).first()
            if quote is None:
                raise Http404
            if quote.user_id is not None and quote.user_id != request.user.pk:
                raise PermissionDenied("This quote belongs to another account.")
            if quote.converted_invoice_id:
                # Account-bound replay works across sessions and key rotation.
                return Invoice.objects.get(pk=quote.converted_invoice_id, user=request.user)
            if not quote.is_acceptable:
                raise forms.ValidationError("This quote can no longer be accepted.")

            def create_result():
                # This write also protects databases without row-level locks:
                # contention fails/rolls back safely rather than duplicating work.
                changed = Quote.objects.filter(pk=quote.pk, status=quote.status, user_id=quote.user_id,
                                               converted_invoice__isnull=True).update(
                    user=request.user, status=Quote.STATUS_ACCEPTED, accepted_at=timezone.now(),
                    accepted_by_ip=get_client_ip(request) or None)
                if changed != 1:
                    raise SubmissionUnavailable("Please retry this acceptance shortly.")
                invoice = convert_quote_to_invoice(quote, by_user=request.user, send_email=False)
                return {"invoice_id": invoice.pk}, {"invoice_id": invoice.pk}

            result, _ = accept_submission(request, "invoice", {"quote_id": quote.pk, "user_id": request.user.pk},
                                          create_result, key=quote.public_token)
            return Invoice.objects.get(pk=result["invoice_id"], user=request.user)
    except (OperationalError, Invoice.DoesNotExist):
        raise SubmissionUnavailable("Please retry this acceptance shortly.") from None
