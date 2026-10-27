from apps.admin_tools.search_keys import SearchKey, FullName
import uuid

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from django.db import models
from django.utils.crypto import salted_hmac
from apps.core.models import TimeStampedModel


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        user = self.create_user(email, password, **extra_fields)

        # Ensure superadmins can sign in immediately even when SMTP is not
        # configured by marking their e-mail as verified in allauth.
        try:
            from allauth.account.models import EmailAddress

            EmailAddress.objects.update_or_create(
                user=user,
                email=user.email,
                defaults={"verified": True, "primary": True},
            )
        except Exception:
            # Keep superuser creation resilient even if allauth tables are
            # unavailable during bootstrap/migrations.
            pass

        return user


class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    phone = models.CharField(max_length=20, blank=True)
    mfa_enabled = models.BooleanField(default=False)
    mfa_secret = models.CharField(max_length=32, blank=True)
    mfa_last_verified_step = models.BigIntegerField(default=-1, editable=False)
    session_version = models.UUIDField(default=uuid.uuid4, editable=False)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        indexes = [
            models.Index(SearchKey("email"), name="search_user_email"),
            models.Index(SearchKey("first_name"), name="search_user_first_name"),
            models.Index(SearchKey("last_name"), name="search_user_last_name"),
            models.Index(SearchKey(FullName("first_name", "last_name")), name="search_user_full_name"),
        ]
        verbose_name = "User"
        verbose_name_plural = "Users"

    def __str__(self):
        return self.email

    def _get_session_auth_hash(self, secret=None):
        # Versioned session binding invalidates pre-fix sessions, including those
        # created through the historical password-only MFA bypass.
        return salted_hmac(
            "accounts.User.session.v2",
            f"{self.password}:{self.session_version}:{self.mfa_enabled}:{self.mfa_secret}",
            secret=secret, algorithm="sha256",
        ).hexdigest()

    def save(self, *args, **kwargs):
        security_fields = {"mfa_secret", "mfa_enabled", "is_active", "is_staff", "is_superuser"}
        update_fields = kwargs.get("update_fields")
        if self.pk and (update_fields is None or security_fields.intersection(update_fields)):
            previous = type(self).objects.filter(pk=self.pk).values(*security_fields).first()
            if previous and any(previous[field] != getattr(self, field) for field in security_fields):
                self.session_version = uuid.uuid4()
                if update_fields is not None:
                    kwargs["update_fields"] = set(update_fields) | {"session_version"}
        return super().save(*args, **kwargs)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip() or self.email


class ClientProfile(TimeStampedModel):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="client_profile")
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    county = models.CharField(max_length=100, blank=True)
    postcode = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=2, default="GB")
    is_business = models.BooleanField(default=False)
    vat_number = models.CharField(max_length=50, blank=True)
    marketing_opted_in = models.BooleanField(default=False)
    stripe_customer_id = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"Profile: {self.user.email}"


class MFABackupCode(TimeStampedModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="mfa_backup_codes")
    code_hash = models.CharField(max_length=255)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "MFA backup code"
        verbose_name_plural = "MFA backup codes"

    def __str__(self):
        return f"Backup code for {self.user.email}"


class MFALoginChallenge(models.Model):
    """A short-lived, single-use primary-authentication result, never a session."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="mfa_challenges")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    credential_hash = models.CharField(max_length=64)
    return_to = models.CharField(max_length=2048, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)


class MFAAttemptWindow(models.Model):
    """Database-backed, per-user throttle shared by workers and sessions."""

    user = models.OneToOneField(User, primary_key=True, on_delete=models.CASCADE)
    started_at = models.DateTimeField()
    attempts = models.PositiveIntegerField(default=0)
