from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.domains.models import RegistrantPublicationDecision
from apps.domains.publication_consent import withdraw_decision


@login_required
@require_GET
def decisions(request):
    page = Paginator(RegistrantPublicationDecision.objects.filter(customer=request.user).order_by("-created_at", "-pk"), 20).get_page(request.GET.get("page"))
    response = render(request, "domains/publication_decisions.html", {"page_obj": page})
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_POST
def withdraw(request, pk):
    try:
        decision = withdraw_decision(request.user, pk)
    except RegistrantPublicationDecision.DoesNotExist:
        raise Http404 from None
    if decision.withdrawal_ticket_id:
        messages.info(request, "Consent withdrawn locally. Support must confirm organization redaction at the registrar; your request is tracked in this ticket.")
        return redirect("support:detail", pk=decision.withdrawal_ticket_id)
    messages.success(request, "Consent withdrawn. Requests that have not reached the registrar will use no publication consent.")
    return redirect("domains:publication_decisions")
