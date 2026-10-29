import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect, render
from django.core.paginator import Paginator
from django.db.models import Q
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from django.http import HttpResponseBadRequest

from .agreements import current_agreement_plan
from .cart_agreements import accept_cart_agreements, cart_agreement_target
from .models import ContactVerification, DomainOrder, DomainTransfer
from .registrar_policy import tld_for_domain
from .request_agreements import accept_request_agreements, request_agreement_target
from apps.portal.models import PortalCartItem

SALT = "cart-agreement-review-v1"


def _require_customer(request):
    if (not request.user.is_active or request.user.is_staff or request.user.is_superuser
            or getattr(request, "impersonation_actor", None)
            or request.session.get("owner_impersonation") or request.session.get("impersonator_user_id")):
        raise PermissionDenied("Agreement acceptance requires the actual customer account.")


@never_cache
@login_required
@require_http_methods(["GET"])
def request_list(request):
    _require_customer(request)
    pages = []
    for kind, model, relation in (("registration", DomainOrder, "registration_attempt"),
                                  ("transfer", DomainTransfer, "submission_attempt")):
        rows = model.objects.filter(user=request.user, domain__isnull=True, registrar_order_id="",
            completed_at__isnull=True, status__in=["draft", "pending_payment", "paid", "processing", "failed"])
        rows = rows.filter(Q(**{relation + "__isnull": True}) | Q(**{
            relation + "__status": "aborted", relation + "__submitted_at__isnull": True,
            relation + "__legacy_review_required": False, relation + "__remote_order_id": ""}))
        rows = rows.only("pk", "domain_name", "status", "agreement_acceptance_id").order_by("-created_at", "-pk")
        pages.append({"kind": kind, "page": Paginator(rows, 20).get_page(request.GET.get(kind + "_page"))})
    cart_items = PortalCartItem.objects.filter(cart__user=request.user, cart__invoice__isnull=True,
        cart__status__in=["active", "quoted"], item_type__in=["domain_registration", "domain_transfer"])
    cart_items = cart_items.select_related("cart").only("pk", "domain_name", "agreement_acceptance_id",
        "cart_id", "cart__status").order_by("-created_at", "-pk")
    pages.append({"kind": "cart", "page": Paginator(cart_items, 20).get_page(request.GET.get("cart_page"))})
    return render(request, "domains/agreement_requests.html", {"request_pages": pages})


class AgreementAcceptanceForm(forms.Form):
    review = forms.CharField(widget=forms.HiddenInput, max_length=4096)
    accepted = forms.BooleanField(label="I am authorised to act for this registrant and accept the agreements displayed above.")

    def clean(self):
        data = super().clean()
        if hasattr(self.data, "getlist") and (set(self.data) - {"csrfmiddlewaretoken", "review", "accepted"}
                or any(len(self.data.getlist(name)) != 1 for name in self.data)):
            raise ValidationError("Submit each agreement field once without additional fields.")
        return data


@never_cache
@login_required
@require_http_methods(["POST"])
def registration_invoice(request, pk):
    _require_customer(request)
    if (set(request.POST) - {"csrfmiddlewaretoken", "review"}
            or any(len(request.POST.getlist(key)) != 1 for key in request.POST)
            or not request.POST.get("review") or len(request.POST["review"]) > 4096):
        return HttpResponseBadRequest("Reload the review page and use its invoice action.")
    from .registration_checkout import create_registration_invoice
    try:
        evidence = signing.loads(request.POST["review"], salt=SALT, max_age=1800)
        if (not isinstance(evidence, dict) or evidence.get("customer") != request.user.pk
                or evidence.get("item") != pk or evidence.get("kind") != "registration"):
            raise ValidationError("Reload this request's agreement review before creating an invoice.")
        invoice = create_registration_invoice(customer=request.user, order_id=pk, context_digest=evidence["context"],
            plan_digest=evidence["plan"], subject_digest=evidence["subject"])
    except (signing.BadSignature, KeyError, TypeError):
        messages.error(request, "The invoice review expired or is invalid. Review the current request again.")
        return redirect("domains:request_agreements", kind="registration", pk=pk)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("domains:request_agreements", kind="registration", pk=pk)
    return redirect("payments:stripe_checkout", invoice_id=invoice.pk)


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def cart_agreements(request, pk, kind="cart"):
    _require_customer(request)
    def target():
        if kind == "cart":
            return cart_agreement_target(request.user, pk)
        return request_agreement_target(request.user, pk, kind=kind)

    if request.method == "POST":
        # Resolve ownership before processing a posted review token.
        try:
            target()
        except ValidationError:
            pass  # Render the scoped hold below; no acceptance will be attached.
    error = None
    if request.method == "POST":
        submitted = AgreementAcceptanceForm(request.POST)
        if submitted.is_valid():
            try:
                evidence = signing.loads(submitted.cleaned_data["review"], salt=SALT, max_age=1800)
                if (not isinstance(evidence, dict) or evidence.get("customer") != request.user.pk
                        or evidence.get("item") != pk or evidence.get("kind") != kind):
                    raise ValidationError("This review belongs to another request. Reload and review the agreements.")
                values = dict(customer=request.user, context_digest=evidence["context"], key=evidence["key"],
                    subject_digest=evidence["subject"], plan_digest=evidence["plan"], accepted=submitted.cleaned_data["accepted"])
                if kind == "cart":
                    accept_cart_agreements(item_id=pk, **values)
                else:
                    accept_request_agreements(request_id=pk, kind=kind, **values)
            except (signing.BadSignature, KeyError, TypeError):
                error = "This review expired or is invalid. Read the current details and agreements again."
            except ValidationError as exc:
                error = " ".join(exc.messages)
            else:
                messages.success(request, "Your agreement acceptance has been recorded for this request.")
                if kind == "cart":
                    return redirect("domains:cart_agreements", pk=pk)
                return redirect("domains:request_agreements", kind=kind, pk=pk)
        else:
            error = "Explicit acceptance is required. Review the details and agreements, then select the checkbox."
    context = {"error": error, "kind": kind}
    try:
        item, contact, operation, digest = target()
        plan = current_agreement_plan(item.domain_name, operation)
        contact.as_resellerclub_payload("1", tld_for_domain(item.domain_name))
        from .us_registry import NEXUS_CHOICES, PURPOSE_CHOICES
        us_declaration = None
        if tld_for_domain(item.domain_name) == "us":
            us_declaration = {"category": dict(NEXUS_CHOICES).get(contact.us_nexus_category),
                              "purpose": dict(PURPOSE_CHOICES).get(contact.us_application_purpose)}
        context.update(item=item, contact=contact, operation=operation, documents=plan.documents,
            us_declaration=us_declaration,
            quote=item.cart.quote if kind == "cart" and item.cart.status == "quoted" else None,
            form=AgreementAcceptanceForm(initial={"review": signing.dumps({"customer": request.user.pk,
                "item": pk, "kind": kind, "context": digest, "key": str(uuid.uuid4()),
                "subject": ContactVerification.digest_for(contact), "plan": plan.digest}, salt=SALT)}))
    except ValidationError as exc:
        context["unavailable"] = " ".join(exc.messages)
    return render(request, "domains/cart_agreements.html", context, status=400 if error else 200)
