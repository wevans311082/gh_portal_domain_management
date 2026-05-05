import pytest

from apps.provisioning.whm_client import WHMClient, WHMClientError


@pytest.mark.django_db
def test_get_quota_uses_quota_module_first(monkeypatch):
    client = WHMClient()
    calls = []

    def fake_cpanel_call(cpanel_username, module, function, params=None):
        calls.append((module, function, cpanel_username))
        if (module, function) == ("Quota", "get_quota_info"):
            return {"data": {"used": "512", "limit": "2048"}}
        raise AssertionError("Unexpected fallback call")

    monkeypatch.setattr(client, "_cpanel_call", fake_cpanel_call)

    result = client.get_quota("acctdemo")

    assert result["used"] == "512"
    assert calls == [("Quota", "get_quota_info", "acctdemo")]


@pytest.mark.django_db
def test_get_quota_falls_back_to_diskusage_when_quota_module_unavailable(monkeypatch):
    client = WHMClient()
    calls = []

    def fake_cpanel_call(cpanel_username, module, function, params=None):
        calls.append((module, function, cpanel_username))
        if (module, function) == ("Quota", "get_quota_info"):
            raise WHMClientError("404")
        if (module, function) == ("DiskUsage", "get_quota"):
            return {"data": {"used": "700", "limit": "4096"}}
        raise AssertionError("Unexpected endpoint")

    monkeypatch.setattr(client, "_cpanel_call", fake_cpanel_call)

    result = client.get_quota("acctdemo")

    assert result["used"] == "700"
    assert calls == [
        ("Quota", "get_quota_info", "acctdemo"),
        ("DiskUsage", "get_quota", "acctdemo"),
    ]


@pytest.mark.django_db
def test_get_quota_raises_when_all_endpoints_fail(monkeypatch):
    client = WHMClient()

    def fake_cpanel_call(cpanel_username, module, function, params=None):
        raise WHMClientError("404")

    monkeypatch.setattr(client, "_cpanel_call", fake_cpanel_call)

    with pytest.raises(WHMClientError):
        client.get_quota("acctdemo")
