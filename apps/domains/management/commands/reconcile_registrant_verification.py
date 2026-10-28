from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.core.runtime_settings import get_runtime_setting
from apps.domains.models import DomainOrder
from apps.domains.resellerclub_client import ResellerClubClient, ResellerClubError
from apps.domains.verification import reconcile_registrant_verification


class Command(BaseCommand):
    help = "Read one linked registrar order and record subject-bound email verification evidence."

    def add_arguments(self, parser):
        parser.add_argument("--order-id", type=int, required=True)

    def handle(self, *args, **options):
        if options["order_id"] <= 0:
            raise CommandError("Choose a positive local domain-order ID.")
        try:
            result = reconcile_registrant_verification(
                options["order_id"], get_runtime_setting("RESELLERCLUB_CUSTOMER_ID", ""),
                client=ResellerClubClient(),
            )
        except DomainOrder.DoesNotExist:
            raise CommandError("The local domain order was not found.") from None
        except (ValidationError, ResellerClubError):
            # Provider exceptions may contain a response body or request details.
            raise CommandError("Verification was not recorded. Check the registrar linkage and try again.") from None
        self.stdout.write(f"Order {options['order_id']}: registrar email {result.status} (evidence {result.pk}).")
