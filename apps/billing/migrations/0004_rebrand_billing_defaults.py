from django.db import migrations


def forwards(apps, schema_editor):
    Branding = apps.get_model("billing", "BillingDocumentBranding")
    for branding in Branding.objects.all():
        changed = []
        if not branding.company_name or branding.company_name in {
            "Grumpy Hosting",
            "Grumpy Hosting LTD",
            "CyberAsk Domains",
            "CyberAsk Domains LTD",
        }:
            branding.company_name = "Cyber Ask Domains"
            changed.append("company_name")
        if not branding.website_url or "grumpyhosting" in branding.website_url.lower():
            branding.website_url = "https://www.cyberask.co.uk/domains"
            changed.append("website_url")
        if not branding.company_number:
            branding.company_number = "15113248"
            changed.append("company_number")
        if (
            not branding.footer_text
            or "Grumpy Hosting" in branding.footer_text
            or "CyberAsk Domains" in branding.footer_text
        ):
            branding.footer_text = "Cyber Ask Domains - a Cyber Ask Ltd service"
            changed.append("footer_text")
        if changed:
            branding.save(update_fields=changed)


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0003_add_invoice_last_dunning_sent_at"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
