import ipaddress

import pytest

from parsedock.clients.addresses import address_allowed, ensure_target_allowed
from parsedock.errors import NetworkError


def allowed(ip: str, *, trust: bool) -> bool:
    return address_allowed(ipaddress.ip_address(ip), trust_private_networks=trust)


def test_public_address_is_allowed_without_trust():
    assert allowed("8.8.8.8", trust=False) is True


def test_private_and_loopback_need_explicit_trust():
    assert allowed("127.0.0.1", trust=False) is False
    assert allowed("127.0.0.1", trust=True) is True
    assert allowed("10.1.1.1", trust=False) is False
    assert allowed("10.1.1.1", trust=True) is True
    assert allowed("192.168.1.10", trust=True) is True
    assert allowed("172.16.5.5", trust=True) is True
    assert allowed("::1", trust=False) is False
    assert allowed("::1", trust=True) is True
    assert allowed("fc00::1", trust=True) is True
    assert allowed("::ffff:127.0.0.1", trust=False) is False
    assert allowed("::ffff:10.0.0.8", trust=True) is True


def test_link_local_multicast_and_documentation_ranges_stay_closed():
    for ip in ("169.254.169.254", "fe80::1", "224.0.0.1", "100.64.1.1", "192.0.2.1", "0.0.0.0"):
        assert allowed(ip, trust=False) is False
        assert allowed(ip, trust=True) is False


def test_host_is_blocked_when_any_resolved_address_is_closed(monkeypatch):
    def resolve(host: str, port: int):
        assert host == "example.test"
        assert port == 80
        return (ipaddress.ip_address("8.8.8.8"), ipaddress.ip_address("10.0.0.1"))

    monkeypatch.setattr("parsedock.clients.addresses.resolve_ips", resolve)

    with pytest.raises(NetworkError, match="10.0.0.1"):
        ensure_target_allowed("http://example.test/item", trust_private_networks=False)


def test_public_resolution_is_allowed(monkeypatch):
    monkeypatch.setattr(
        "parsedock.clients.addresses.resolve_ips",
        lambda host, port: (ipaddress.ip_address("8.8.8.8"),),
    )

    ensure_target_allowed("https://example.test/item", trust_private_networks=False)
