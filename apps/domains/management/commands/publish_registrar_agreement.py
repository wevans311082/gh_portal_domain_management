"""Publish exact operator-approved text without fabricating customer acceptance."""

import json
from pathlib import Path

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.domains.agreements import publish_agreement


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


class Command(BaseCommand):
    help = "Publish an approved registrar agreement JSON file. Operator shell access is the authentication boundary."

    def add_arguments(self, parser):
        parser.add_argument("--owner-id", type=int, required=True)
        parser.add_argument("--document-file", required=True)
        parser.add_argument("--approved", action="store_true", help="Confirm approval of the exact text and selected profile/operation scope.")

    def handle(self, *args, **options):
        if not options["approved"]:
            raise CommandError("Review and approve the exact document before using --approved.")
        try:
            owner = User.objects.get(pk=options["owner_id"], is_active=True, is_staff=True, is_superuser=True)
        except User.DoesNotExist:
            raise CommandError("An active owner is required for publication attribution.") from None
        try:
            with Path(options["document_file"]).open("rb") as document_file:
                raw = document_file.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise ValueError("Too large")
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            required = {"agreement_key", "version", "title", "body", "profile_keys", "operations", "submission_key"}
            if not isinstance(data, dict) or set(data) - required - {"source_url"} or not required <= set(data):
                raise ValueError("Unexpected structure")
        except (OSError, UnicodeError, ValueError, RecursionError):
            raise CommandError("Supply a UTF-8 JSON document of at most 256 KiB with the required unique fields. No agreement was published.") from None
        try:
            document = publish_agreement(actor=owner, reviewed=True, **data)
        except PermissionDenied:
            raise CommandError("Owner publication access is no longer available.") from None
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from None
        self.stdout.write(f"Agreement {document.pk}, version {document.version}: published. Customer acceptance was not inferred.")
