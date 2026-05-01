"""Invoice views."""
import logging
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.template.loader import render_to_string

from apps.billing.models import Invoice

logger = logging.getLogger(__name__)

_PAGE_SIZE = 20


@login_required
def invoice_list(request):
    """List all invoices for the current user — paginated."""
    qs = Invoice.objects.filter(user=request.user).order_by("-created_at")
    paginator = Paginator(qs, _PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "invoices/list.html", {"invoices": page_obj.object_list, "page_obj": page_obj})


@login_required
def invoice_detail(request, pk):
    """View a specific invoice."""
    invoice = get_object_or_404(Invoice, pk=pk, user=request.user)
    return render(request, "invoices/detail.html", {"invoice": invoice})


@login_required
def invoice_pdf(request, pk):
    """Download invoice as PDF using WeasyPrint."""
    invoice = get_object_or_404(Invoice, pk=pk, user=request.user)

    try:
        from weasyprint import HTML
        html_string = render_to_string("invoices/pdf.html", {
            "invoice": invoice,
            "request": request,
        })
        html = HTML(string=html_string, base_url=request.build_absolute_uri())
        pdf_bytes = html.write_pdf()

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="invoice-{invoice.number}.pdf"'
        return response
    except ImportError:
        logger.warning("WeasyPrint not available, returning HTML invoice")
        html_string = render_to_string("invoices/pdf.html", {
            "invoice": invoice,
            "request": request,
        })
        return HttpResponse(html_string, content_type="text/html")
