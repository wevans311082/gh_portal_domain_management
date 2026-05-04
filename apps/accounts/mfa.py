import secrets
from typing import List

from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone

from apps.accounts.models import MFABackupCode, User


def generate_plain_backup_codes(count: int = 10) -> List[str]:
    codes: List[str] = []
    for _ in range(count):
        raw = secrets.token_hex(4).upper()
        codes.append(f"{raw[:4]}-{raw[4:]}")
    return codes


def regenerate_backup_codes(user: User, count: int = 10) -> List[str]:
    plain_codes = generate_plain_backup_codes(count=count)
    MFABackupCode.objects.filter(user=user).delete()
    MFABackupCode.objects.bulk_create(
        [
            MFABackupCode(user=user, code_hash=make_password(code))
            for code in plain_codes
        ]
    )
    return plain_codes


def consume_backup_code(user: User, provided_code: str) -> bool:
    normalized = (provided_code or "").strip().upper().replace(" ", "")
    if len(normalized) == 8 and "-" not in normalized:
        normalized = f"{normalized[:4]}-{normalized[4:]}"

    for backup in MFABackupCode.objects.filter(user=user, used_at__isnull=True).order_by("id"):
        if check_password(normalized, backup.code_hash):
            backup.used_at = timezone.now()
            backup.save(update_fields=["used_at", "updated_at"])
            return True
    return False


def active_backup_code_count(user: User) -> int:
    return MFABackupCode.objects.filter(user=user, used_at__isnull=True).count()
