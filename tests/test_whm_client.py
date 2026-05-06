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


@pytest.mark.django_db
def test_list_accounts_can_request_specific_columns(monkeypatch):
    client = WHMClient()
    captured = {}

    def fake_call(function, params=None):
        captured["function"] = function
        captured["params"] = params or {}
        return {"data": {"acct": [{"user": "alice", "domain": "example.com"}]}}

    monkeypatch.setattr(client, "_call", fake_call)

    result = client.list_accounts(columns=["user", "domain"])

    assert result[0]["user"] == "alice"
    assert captured["function"] == "listaccts"
    assert captured["params"]["api.columns.a"] == "user"
    assert captured["params"]["api.columns.b"] == "domain"
    assert captured["params"]["api.columns.enable"] == 1


@pytest.mark.django_db
def test_modify_account_calls_modifyacct(monkeypatch):
    client = WHMClient()
    captured = {}

    def fake_call(function, params=None):
        captured["function"] = function
        captured["params"] = params or {}
        return {"ok": True}

    monkeypatch.setattr(client, "_call", fake_call)

    client.modify_account("alice", domain="example.net", contact_email="ops@example.net")

    assert captured["function"] == "modifyacct"
    assert captured["params"]["user"] == "alice"
    assert captured["params"]["domain"] == "example.net"
    assert captured["params"]["contactemail"] == "ops@example.net"


@pytest.mark.django_db
def test_change_password_calls_passwd(monkeypatch):
    client = WHMClient()
    captured = {}

    def fake_call(function, params=None):
        captured["function"] = function
        captured["params"] = params or {}
        return {"ok": True}

    monkeypatch.setattr(client, "_call", fake_call)

    client.change_password("alice", "super-secret")

    assert captured["function"] == "passwd"
    assert captured["params"] == {"user": "alice", "password": "super-secret"}


@pytest.mark.django_db
def test_list_users_parses_response_shape(monkeypatch):
    client = WHMClient()

    monkeypatch.setattr(client, "_call", lambda function, params=None: {"data": {"users": ["root", "alice"]}})

    users = client.list_users()

    assert users == ["root", "alice"]


@pytest.mark.django_db
def test_dns_zone_record_helpers_call_expected_endpoints(monkeypatch):
    client = WHMClient()
    calls = []

    def fake_call(function, params=None):
        calls.append((function, params or {}))
        return {"ok": True}

    monkeypatch.setattr(client, "_call", fake_call)

    client.add_zone_record("example.com", "www", "A", "1.2.3.4", ttl=300)
    client.edit_zone_record("example.com", 42, "www", "A", "5.6.7.8", ttl=600)
    client.remove_zone_record("example.com", 42)

    assert calls[0][0] == "addzonerecord"
    assert calls[0][1]["domain"] == "example.com"
    assert calls[0][1]["ttl"] == 300
    assert calls[1][0] == "editzonerecord"
    assert calls[1][1]["line"] == 42
    assert calls[2] == ("removezonerecord", {"zone": "example.com", "line": 42})


@pytest.mark.django_db
def test_package_management_helpers_call_expected_endpoints(monkeypatch):
    client = WHMClient()
    calls = []

    def fake_call(function, params=None):
        calls.append((function, params or {}))
        return {"ok": True}

    monkeypatch.setattr(client, "_call", fake_call)

    client.create_package("starter", {"quota": "2048", "bwlimit": "51200"})
    client.update_package("starter", {"quota": "4096"})
    client.delete_package("starter")

    assert calls[0] == ("addpkg", {"name": "starter", "quota": "2048", "bwlimit": "51200"})
    assert calls[1] == ("editpkg", {"name": "starter", "quota": "4096"})
    assert calls[2] == ("killpkg", {"pkg": "starter"})


@pytest.mark.django_db
def test_get_quota_skips_known_unsupported_fallback_endpoint(monkeypatch):
    WHMClient._uapi_support_cache.clear()
    client = WHMClient()
    calls = []

    def fake_cpanel_call(cpanel_username, module, function, params=None):
        calls.append((module, function))
        if (module, function) == ("Quota", "get_quota_info"):
            raise WHMClientError("404")
        if (module, function) == ("DiskUsage", "get_quota"):
            raise WHMClientError("404")
        raise AssertionError("Unexpected endpoint")

    monkeypatch.setattr(client, "_cpanel_call", fake_cpanel_call)

    with pytest.raises(WHMClientError):
        client.get_quota("acctdemo")

    calls.clear()
    with pytest.raises(WHMClientError):
        client.get_quota("acctdemo")

    # After first 404 detection, both endpoints are cached unsupported and not retried.
    assert calls == []
