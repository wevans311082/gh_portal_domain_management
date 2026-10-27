from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import ClientProfile, MFABackupCode, User
from django.core.exceptions import PermissionDenied


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ["email", "first_name", "last_name", "is_active", "is_staff", "created_at"]
    list_filter = ["is_active", "is_staff", "mfa_enabled"]
    search_fields = ["email", "first_name", "last_name"]
    ordering = ["-created_at"]
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal Info", {"fields": ("first_name", "last_name", "phone")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("MFA", {"fields": ("mfa_enabled",)}),
        ("Dates", {"fields": ("last_login",)}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "password1", "password2"),
        }),
    )
    filter_horizontal = ("groups", "user_permissions")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        # TOTP secrets are enrolled by their owner, not entered in raw admin forms.
        fields.append("mfa_enabled")
        if obj and obj.pk == request.user.pk:
            fields.extend(["is_active", "is_staff", "is_superuser", "groups", "user_permissions"])
        return list(dict.fromkeys(fields))

    def has_delete_permission(self, request, obj=None):
        return not (obj and obj.pk == request.user.pk) and super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        if queryset.filter(pk=request.user.pk).exists():
            raise PermissionDenied("You cannot delete your own owner account.")
        return super().delete_queryset(request, queryset)


@admin.register(ClientProfile)
class ClientProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "city", "country", "is_business"]
    search_fields = ["user__email", "city", "postcode"]


@admin.register(MFABackupCode)
class MFABackupCodeAdmin(admin.ModelAdmin):
    list_display = ["user", "used_at", "created_at"]
    list_filter = ["used_at", "created_at"]
    search_fields = ["user__email"]
    readonly_fields = ["code_hash", "created_at", "updated_at", "used_at"]
