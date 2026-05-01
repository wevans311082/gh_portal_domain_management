from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WebsiteTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                ("slug", models.SlugField(max_length=130, unique=True)),
                ("category", models.CharField(
                    choices=[
                        ("hosting", "Hosting / Tech"),
                        ("business", "Business / Corporate"),
                        ("portfolio", "Portfolio / CV"),
                        ("restaurant", "Restaurant / Food"),
                        ("wedding", "Wedding / Events"),
                        ("photography", "Photography"),
                        ("ecommerce", "E-Commerce / Shop"),
                        ("construction", "Under Construction"),
                        ("blog", "Blog / Magazine"),
                        ("other", "Other"),
                    ],
                    default="other",
                    max_length=30,
                )),
                ("description", models.TextField(blank=True)),
                ("zip_filename", models.CharField(blank=True, max_length=200)),
                ("extracted_path", models.CharField(blank=True, max_length=300)),
                ("has_index", models.BooleanField(default=True)),
                ("security_notes", models.TextField(blank=True)),
                ("jquery_version", models.CharField(blank=True, max_length=20)),
                ("bootstrap_version", models.CharField(blank=True, max_length=20)),
                ("is_sanitised", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["category", "name"], "verbose_name": "Website Template", "verbose_name_plural": "Website Templates"},
        ),
        migrations.CreateModel(
            name="TemplateInstallation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("service_domain", models.CharField(blank=True, max_length=253)),
                ("status", models.CharField(
                    choices=[("active", "Active"), ("removed", "Removed")],
                    default="active",
                    max_length=20,
                )),
                ("installed_at", models.DateTimeField(auto_now_add=True)),
                ("removed_at", models.DateTimeField(blank=True, null=True)),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="template_installations",
                    to=settings.AUTH_USER_MODEL,
                )),
                ("template", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="installations",
                    to="website_templates.websitetemplate",
                )),
            ],
            options={"ordering": ["-installed_at"], "verbose_name": "Template Installation"},
        ),
    ]
