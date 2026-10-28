from django.core.management.base import BaseCommand, CommandError

from apps.domains.contact_change_recovery import recover_contact_change, confirm_provider_completed


class Command(BaseCommand):
    help = "Audited owner contact-change recovery. Never resubmits changes or applies unconfirmed contacts."

    def add_arguments(self, parser):
        parser.add_argument("--change-id", type=int, required=True)
        parser.add_argument("--owner-id", type=int, required=True)
        parser.add_argument("--action", choices=["link-action", "cancel-preparation", "confirm-provider-completed"], required=True)
        parser.add_argument("--provider-success-confirmed", action="store_true")
        parser.add_argument("--action-id", default="")
        parser.add_argument("--provider-reference", required=True)
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **options):
        from apps.domains.resellerclub_client import ResellerClubClient
        try:
            if options["action"] == "confirm-provider-completed":
                if options["action_id"]:
                    raise ValueError("Completion uses the retained action ID.")
                status = confirm_provider_completed(options["change_id"], owner_id=options["owner_id"],
                    reference=options["provider_reference"], reason=options["reason"],
                    provider_success_confirmed=options["provider_success_confirmed"], client=ResellerClubClient())
                self.stdout.write(self.style.SUCCESS("Contact-change recovery recorded: " + status))
                return
            if options["provider_success_confirmed"]:
                raise ValueError("Choose the explicit completion action for success attestation.")
            status = recover_contact_change(options["change_id"], owner_id=options["owner_id"], action=options["action"],
                action_id=options["action_id"], reference=options["provider_reference"], reason=options["reason"],
                client=ResellerClubClient() if options["action"] == "link-action" else None)
        except Exception:
            # Provider errors can contain credentials, URLs or contact details.
            raise CommandError("Contact-change recovery was not applied. Check owner access, worker state, retained IDs and provider evidence.") from None
        self.stdout.write(self.style.SUCCESS("Contact-change recovery recorded: " + status))
