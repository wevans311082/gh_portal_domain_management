from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from .models import SupportTicket, SupportTicketMessage, Department


@login_required
def ticket_list(request):
    tickets = SupportTicket.objects.filter(user=request.user)
    return render(request, "support/ticket_list.html", {"tickets": tickets})


@login_required
def ticket_detail(request, pk):
    ticket = get_object_or_404(SupportTicket, pk=pk, user=request.user)
    if request.method == "POST":
        content = request.POST.get("content", "").strip()
        if content:
            SupportTicketMessage.objects.create(ticket=ticket, user=request.user, content=content)
            messages.success(request, "Reply sent.")
            return redirect("support:ticket_detail", pk=pk)
    return render(request, "support/ticket_detail.html", {"ticket": ticket})


@login_required
def ticket_create(request):
    departments = Department.objects.filter(is_active=True)
    if request.method == "POST":
        subject = request.POST.get("subject", "").strip()
        content = request.POST.get("content", "").strip()
        department_id = request.POST.get("department")
        if subject and content:
            dept = Department.objects.filter(id=department_id).first() if department_id else None
            ticket = SupportTicket.objects.create(user=request.user, subject=subject, department=dept)
            SupportTicketMessage.objects.create(ticket=ticket, user=request.user, content=content)
            messages.success(request, "Ticket created.")
            return redirect("support:ticket_detail", pk=ticket.pk)
    return render(request, "support/ticket_create.html", {"departments": departments})
