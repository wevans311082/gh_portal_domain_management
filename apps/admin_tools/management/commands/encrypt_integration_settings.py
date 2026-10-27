"""Resumable conversion/rotation without emitting credentials or key material."""
from django.core.management.base import BaseCommand, CommandError
from django.core.cache import cache
from django.db import transaction

from apps.admin_tools.models import IntegrationSetting
from apps.admin_tools.secrets import PREFIX, IntegrationSecretError, decrypt_value, keyring


class Command(BaseCommand):
    help = "Inspect integration storage; use --apply to encrypt legacy rows, --rotate to rewrap old key versions."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--rotate", action="store_true")
        parser.add_argument("--check", action="store_true", help="Exit unsuccessfully if legacy/old-key rows remain.")
        parser.add_argument("--batch-size", type=int, default=100)

    def handle(self, *args, **options):
        if not 1 <= options["batch_size"] <= 1000:
            raise CommandError("Batch size must be between 1 and 1000.")
        if options["apply"] and options["check"]:
            raise CommandError("Use --check separately after --apply.")
        try:
            active, keys = keyring()
        except IntegrationSecretError as error:
            raise CommandError(str(error)) from None
        counts = {"legacy": 0, "old_key": 0, "current": 0, "converted": 0}
        last_pk = 0
        while True:
            ids = list(IntegrationSetting.objects.filter(pk__gt=last_pk).order_by("pk").values_list("pk", flat=True)[:options["batch_size"]])
            if not ids:
                break
            for identifier in ids:
                try:
                    with transaction.atomic():
                        row = IntegrationSetting.objects.select_for_update().get(pk=identifier)
                        legacy = not row.value.startswith(PREFIX)
                        if legacy:
                            plaintext = row.value
                            counts["legacy"] += 1
                        else:
                            plaintext = decrypt_value(row.key, row.value)
                            old_key = not row.value.startswith(f"{PREFIX}{active}:")
                            counts["old_key" if old_key else "current"] += 1
                        if options["apply"] and (legacy or (options["rotate"] and old_key)):
                            row.value = plaintext
                            row.save(update_fields=["value", "updated_at"])
                            row.refresh_from_db()
                            if decrypt_value(row.key, row.value) != plaintext:
                                raise IntegrationSecretError("Encrypted storage verification failed.")
                            counts["converted"] += 1
                        if options["apply"]:
                            cache.delete(f"runtime_setting:{row.key}")
                except IntegrationSecretError:
                    raise CommandError(f"Protected storage verification failed at row {identifier}. Earlier committed rows can be resumed safely.") from None
            last_pk = ids[-1]
        self.stdout.write(" ".join(f"{key}={value}" for key, value in counts.items()))
        if not options["apply"]:
            self.stdout.write("Read-only inspection; no settings changed.")
        if options["check"] and (counts["legacy"] or counts["old_key"]):
            raise CommandError("Protected storage conversion/rotation is incomplete.")
