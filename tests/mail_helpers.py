"""Explicit worker execution for isolated account-mail integration tests."""
def deliver_account_mail():
    from apps.core.models import PublicDelivery
    from apps.core.tasks import deliver_public_submission
    identifiers = list(PublicDelivery.objects.filter(submission__kind="account", state="queued").values_list("pk", flat=True))
    for identifier in identifiers:
        assert deliver_public_submission(str(identifier)) in {"sent", "cancelled"}
