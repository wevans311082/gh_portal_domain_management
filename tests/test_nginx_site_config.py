from pathlib import Path
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.provisioning.nginx import NginxSiteConfigError, NginxSiteConfigService, NginxTlsOptions


TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "nginx" / "templates" / "site.conf.j2"


def test_apply_site_config_validates_before_reload(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, capture_output, text, check):
        calls.append(command)
        return Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("apps.provisioning.nginx.subprocess.run", fake_run)
    service = NginxSiteConfigService(
        template_path=TEMPLATE_PATH,
        sites_dir=tmp_path,
        validate_command=["nginx", "-t"],
        reload_command=["nginx", "-s", "reload"],
    )

    destination = service.apply_site_config(
        domain="Customer.Example.com",
        container_name="customer_site_1",
        internal_port=8080,
        tls=NginxTlsOptions(enabled=False),
    )

    assert calls == [["nginx", "-t"], ["nginx", "-s", "reload"]]
    assert destination == tmp_path / "customer.example.com.conf"
    rendered = destination.read_text(encoding="utf-8")
    assert "server_name customer.example.com;" in rendered
    assert "proxy_pass http://customer_site_1:8080;" in rendered
    assert "proxy_pass http://django" not in rendered


def test_apply_site_config_rolls_back_and_skips_reload_when_validation_fails(monkeypatch, tmp_path):
    destination = tmp_path / "customer.example.com.conf"
    destination.write_text("previous config", encoding="utf-8")
    calls = []

    def fake_run(command, capture_output, text, check):
        calls.append(command)
        return Mock(returncode=1, stdout="", stderr="nginx failed")

    monkeypatch.setattr("apps.provisioning.nginx.subprocess.run", fake_run)
    service = NginxSiteConfigService(
        template_path=TEMPLATE_PATH,
        sites_dir=tmp_path,
        validate_command=["nginx", "-t"],
        reload_command=["nginx", "-s", "reload"],
    )

    with pytest.raises(NginxSiteConfigError, match="nginx validation failed"):
        service.apply_site_config(
            domain="customer.example.com",
            container_name="customer_site_1",
            internal_port=8080,
        )

    assert calls == [["nginx", "-t"]]
    assert destination.read_text(encoding="utf-8") == "previous config"


def test_render_site_config_requires_tls_certificate_paths():
    service = NginxSiteConfigService(template_path=TEMPLATE_PATH)

    with pytest.raises(NginxSiteConfigError, match="certificate and key paths"):
        service.render_site_config(
            domain="customer.example.com",
            container_name="customer_site_1",
            internal_port=80,
            tls=NginxTlsOptions(enabled=True),
        )


def test_render_nginx_site_management_command_uses_service(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, capture_output, text, check):
        calls.append(command)
        return Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("apps.provisioning.nginx.subprocess.run", fake_run)

    call_command(
        "render_nginx_site",
        domain="customer.example.com",
        container_name="customer_site_1",
        internal_port=8080,
        sites_dir=str(tmp_path),
        validate_command=["nginx", "-t"],
        reload_command=["nginx", "-s", "reload"],
    )

    assert calls == [["nginx", "-t"], ["nginx", "-s", "reload"]]
    assert (tmp_path / "customer.example.com.conf").exists()


def test_render_nginx_site_management_command_reports_validation_errors(monkeypatch, tmp_path):
    def fake_run(command, capture_output, text, check):
        return Mock(returncode=1, stdout="", stderr="nginx failed")

    monkeypatch.setattr("apps.provisioning.nginx.subprocess.run", fake_run)

    with pytest.raises(CommandError, match="nginx validation failed"):
        call_command(
            "render_nginx_site",
            domain="customer.example.com",
            container_name="customer_site_1",
            internal_port=8080,
            sites_dir=str(tmp_path),
            validate_command=["nginx", "-t"],
            reload_command=["nginx", "-s", "reload"],
        )

    assert not (tmp_path / "customer.example.com.conf").exists()


def test_portal_default_route_is_configured_separately_from_site_includes():
    nginx_conf = (Path(__file__).resolve().parents[1] / "nginx" / "nginx.conf").read_text(
        encoding="utf-8"
    )

    assert "listen 80 default_server;" in nginx_conf
    assert "proxy_pass http://django;" in nginx_conf
    assert nginx_conf.index("listen 80 default_server;") < nginx_conf.index(
        "include /etc/nginx/conf.d/sites/*.conf;"
    )
