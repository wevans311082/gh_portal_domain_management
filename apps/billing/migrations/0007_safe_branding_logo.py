import apps.billing.uploads
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("billing", "0006_branding_cyber_ask_domains_name")]
    operations = [migrations.AlterField(
        model_name="billingdocumentbranding", name="logo",
        field=apps.billing.uploads.SafeLogoField(
            blank=True, null=True, upload_to="billing/branding/",
            validators=[apps.billing.uploads.validate_logo],
            help_text="Upload PNG, JPEG or WebP, up to 5 MB, 4096 pixels per side and 8 million pixels total. Logos are stored as PNG.",
        ),
    )]
