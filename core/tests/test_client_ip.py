import pytest
from django.test import RequestFactory, override_settings

from flarize.client_ip import get_client_ip, parse_ip, parse_networks, resolve_client_ip

TRUSTED = parse_networks(("10.0.0.0/8", "fd00::/8"))


@pytest.mark.parametrize(
    ("remote", "xff", "expected"),
    [
        # direct client: XFF is attacker-controlled and ignored
        ("203.0.113.9", "1.1.1.1", "203.0.113.9"),
        ("203.0.113.9", None, "203.0.113.9"),
        # one trusted proxy
        ("10.0.0.1", "198.51.100.7", "198.51.100.7"),
        # client prepends a spoofed hop: the proxy-appended one wins
        ("10.0.0.1", "1.1.1.1, 198.51.100.7", "198.51.100.7"),
        # chain of trusted proxies
        ("10.0.0.1", "198.51.100.7, 10.0.0.2, 10.0.0.3", "198.51.100.7"),
        # trusted proxy but no header
        ("10.0.0.1", None, "10.0.0.1"),
        ("10.0.0.1", "", "10.0.0.1"),
        # everything trusted: leftmost vouched address
        ("10.0.0.1", "10.0.0.5", "10.0.0.5"),
        # garbage hop stops the walk at the last vouched address
        ("10.0.0.1", "198.51.100.7, not-an-ip", "10.0.0.1"),
        ("10.0.0.1", "not-an-ip, 10.0.0.9", "10.0.0.9"),
        # ports and IPv6 forms
        ("10.0.0.1", "198.51.100.7:4711", "198.51.100.7"),
        ("fd00::1", "[2001:db8::5]:443", "2001:db8::5"),
        ("::ffff:10.0.0.1", "198.51.100.7", "198.51.100.7"),
        # malformed peer
        ("", "198.51.100.7", ""),
    ],
)
def test_resolve(remote, xff, expected):
    assert resolve_client_ip(remote, xff, TRUSTED) == expected


def test_nothing_is_trusted_without_trusted_proxies():
    assert resolve_client_ip("10.0.0.1", "198.51.100.7", ()) == "10.0.0.1"


def test_parse_networks_rejects_invalid_cidrs():
    with pytest.raises(ValueError):
        parse_networks(("10.0.0.0/33",))
    assert parse_networks(("", " 192.168.0.0/16 ")) == (parse_networks(("192.168.0.0/16",))[0],)


def test_parse_ip():
    assert str(parse_ip(" 1.2.3.4 ")) == "1.2.3.4"
    assert parse_ip("nonsense") is None
    assert parse_ip(None) is None


@override_settings(TRUSTED_PROXIES=["10.0.0.0/8"])
def test_get_client_ip_reads_meta_and_memoises():
    request = RequestFactory().get("/", REMOTE_ADDR="10.1.2.3", HTTP_X_FORWARDED_FOR="5.6.7.8")
    assert get_client_ip(request) == "5.6.7.8"
    request.META["HTTP_X_FORWARDED_FOR"] = "9.9.9.9"
    assert get_client_ip(request) == "5.6.7.8"


@override_settings(TRUSTED_PROXIES=[])
def test_get_client_ip_ignores_xff_when_nothing_is_trusted():
    request = RequestFactory().get("/", REMOTE_ADDR="10.1.2.3", HTTP_X_FORWARDED_FOR="5.6.7.8")
    assert get_client_ip(request) == "10.1.2.3"
