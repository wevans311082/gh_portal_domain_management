"""Render, validate, and activate a customer website nginx vhost."""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.provisioning.nginx import NginxSiteConfigError, NginxSiteConfigService, NginxTlsOptions


class Command(BaseCommand):
    help = "Render a per-site nginx config, validate nginx, and reload nginx after validation."

    def add_arguments(self, parser):
        parser.add_argument("--domain", required=True, help="Customer website domain name.")
        parser.add_argument(
            "--container-name", required=True, help="Docker container name nginx should proxy to."
        )
        parser.add_argument(
            "--internal-port",
            type=int,
            required=True,
            help="Internal container port nginx should proxy to.",
        )
        parser.add_argument("--tls-enabled", action="store_true", help="Render an HTTPS vhost.")
        parser.add_argument("--tls-certificate-path", default="", help="Path to the TLS certificate.")
        parser.add_argument("--tls-certificate-key-path", default="", help="Path to the TLS private key.")
        parser.add_argument(
            "--force-https",
            action="store_true",
            help="Redirect HTTP requests to HTTPS when TLS is enabled.",
        )
        parser.add_argument("--template-path", default=None, help="Override the nginx vhost template path.")
        parser.add_argument("--sites-dir", default=None, help="Override the destination sites directory.")
        parser.add_argument(
            "--validate-command",
            nargs="+",
            default=None,
            help="Override the validation command (default: nginx -t).",
        )
        parser.add_argument(
            "--reload-command",
            nargs="+",
            default=None,
            help="Override the reload command (default: nginx -s reload).",
        )

    def handle(self, *args, **options):
        tls = NginxTlsOptions(
            enabled=options["tls_enabled"],
            certificate_path=options["tls_certificate_path"],
            certificate_key_path=options["tls_certificate_key_path"],
            force_https=options["force_https"],
        )
        service = NginxSiteConfigService(
            template_path=options["template_path"],
            sites_dir=options["sites_dir"],
            validate_command=options["validate_command"],
            reload_command=options["reload_command"],
        )

        try:
            destination = service.apply_site_config(
                domain=options["domain"],
                container_name=options["container_name"],
                internal_port=options["internal_port"],
                tls=tls,
            )
        except NginxSiteConfigError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"Activated nginx site config: {destination}"))
