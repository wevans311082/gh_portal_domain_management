"""Read-only transfer workbench; provider recovery remains an audited owner command."""

from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from apps.admin_tools.decorators import staff_member_required
from apps.domains.models import DomainTransfer


FIELDS = ("pk", "domain_name", "status", "created_at", "updated_at", "domain_id",
          "submission_attempt__pk", "submission_attempt__status", "submission_attempt__legacy_review_required",
          "submission_attempt__submitted_at", "submission_attempt__completed_at", "submission_attempt__updated_at")
LABELS = {"preparing": "Preparing contacts", "submitting": "Submission started; do not repeat",
          "unknown": "Owner review required", "accepted": "Transfer order acknowledged",
          "aborted": "Stopped before paid submission"}


def _present(row):
    return {"pk": row["pk"], "domain_name": row["domain_name"], "status": row["status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"], "domain_id": row["domain_id"],
            "attempt_id": row["submission_attempt__pk"],
            "attempt_status": row["submission_attempt__status"],
            "attempt_label": LABELS.get(row["submission_attempt__status"], "Historical record; no submission evidence retained"),
            "historical_review": row["submission_attempt__legacy_review_required"],
            "submitted_at": row["submission_attempt__submitted_at"], "state_updated_at": row["submission_attempt__updated_at"],
            "acknowledged_at": row["submission_attempt__completed_at"]}


def _response(request, template, context):
    response = render(request, template, context)
    response["Cache-Control"] = "no-store"
    return response


@staff_member_required
@require_GET
def domain_transfers_list(request):
    status = request.GET.get("state", "all")
    allowed = {"all", "historical", *LABELS}
    if status not in allowed:
        return _response(request, "admin_tools/ops/transfers_list.html", {"filter_error": "Choose a listed submission state.", "states": LABELS, "state": "all"})
    rows = DomainTransfer.objects.order_by("-created_at", "-pk")
    if status == "historical":
        rows = rows.filter(submission_attempt__legacy_review_required=True)
    elif status != "all":
        rows = rows.filter(submission_attempt__status=status)
    page = Paginator(rows.values(*FIELDS), 20).get_page(request.GET.get("page"))
    return _response(request, "admin_tools/ops/transfers_list.html", {
        "transfers": [_present(row) for row in page], "page_obj": page, "states": LABELS, "state": status,
    })


@staff_member_required
@require_GET
def domain_transfer_detail(request, pk):
    row = get_object_or_404(DomainTransfer.objects.values(*FIELDS), pk=pk)
    return _response(request, "admin_tools/ops/transfer_detail.html", {"transfer": _present(row)})
