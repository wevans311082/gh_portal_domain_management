"""Support ticket views."""
import logging
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.paginator import Paginator
from django import forms

from apps.support.models import SupportTicket, SupportTicketMessage, Department

logger = logging.getLogger(__name__)

_PAGE_SIZE = 20


class TicketForm(forms.ModelForm):
    message = forms.CharField(widget=forms.Textarea(attrs={"rows": 5}), label="Message")

    class Meta:
        model = SupportTicket
        fields = ["subject", "department", "priority"]


class TicketReplyForm(forms.ModelForm):
    class Meta:
        model = SupportTicketMessage
        fields = ["content", "attachment"]
        widgets = {
            "content": forms.Textarea(attrs={"rows": 4}),
        }


@login_required
def ticket_list(request):
    """List support tickets for current user — paginated."""
    qs = SupportTicket.objects.filter(user=request.user).order_by("-created_at")
    paginator = Paginator(qs, _PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "support/ticket_list.html", {"tickets": page_obj.object_list, "page_obj": page_obj})


@login_required
def ticket_create(request):
    """Create a new support ticket."""
    if request.method == "POST":
        form = TicketForm(request.POST)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.user = request.user
            ticket.save()

            message_content = request.POST.get("message", "")
            if message_content:
                SupportTicketMessage.objects.create(
                    ticket=ticket,
                    user=request.user,
                    content=message_content,
                )

            messages.success(request, f"Ticket #{ticket.id} created successfully.")
            return redirect("support:detail", pk=ticket.id)
    else:
        form = TicketForm()

    departments = Department.objects.filter(is_active=True)
    return render(request, "support/ticket_create.html", {"form": form, "departments": departments})


@login_required
def ticket_detail(request, pk):
    """View and reply to a support ticket."""
    ticket = get_object_or_404(SupportTicket, pk=pk, user=request.user)
    ticket_messages = ticket.messages.filter(is_internal=False)

    if request.method == "POST":
        form = TicketReplyForm(request.POST, request.FILES)
        if form.is_valid():
            reply = form.save(commit=False)
            reply.ticket = ticket
            reply.user = request.user
            reply.save()

            ticket.status = SupportTicket.STATUS_AWAITING_SUPPORT
            ticket.save(update_fields=["status"])

            messages.success(request, "Reply sent.")
            return redirect("support:detail", pk=pk)
    else:
        form = TicketReplyForm()

    return render(request, "support/ticket_detail.html", {
        "ticket": ticket,
        "ticket_messages": ticket_messages,
        "form": form,
    })
