import pytest
from allauth.account.models import EmailAddress


@pytest.mark.django_db
def test_create_superuser_marks_email_verified(django_user_model):
    user = django_user_model.objects.create_superuser(
        email="admin@example.com",
        password="SuperSecret123!",
    )

    assert user.is_staff is True
    assert user.is_superuser is True

    email_record = EmailAddress.objects.get(user=user, email="admin@example.com")
    assert email_record.verified is True
    assert email_record.primary is True
