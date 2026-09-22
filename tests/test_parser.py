import json

import pytest

from caddyparser.parser import ParseError, normalize_host, normalize_path, parse_line


def record(**overrides):
    value = {
        "ts": 1_700_000_000.25,
        "request": {
            "remote_ip": "192.0.2.1",
            "host": "Example.COM:443",
            "uri": "/docs//guide/?token=secret",
            "headers": {"Authorization": ["secret"]},
        },
        "status": 200,
        "size": 42,
    }
    value.update(overrides)
    return json.dumps(value)


def test_parses_only_analytics_fields():
    event = parse_line(record(), b"k" * 32)
    assert event.host == "example.com"
    assert event.path == "/docs/guide"
    assert event.first_path == "/docs"
    assert event.bytes_sent == 42
    assert len(event.visitor) == 16
    assert b"192.0.2.1" not in event.visitor


@pytest.mark.parametrize(
    ("value", "expected"),
    [("[2001:db8::1]:443", "2001:db8::1"), ("EXAMPLE.org", "example.org")],
)
def test_normalizes_host(value, expected):
    assert normalize_host(value) == expected


@pytest.mark.parametrize(
    ("uri", "expected"),
    [("/", ("/", "/")), ("/caf%C3%A9/?x=1", ("/café", "/café")), ("a//b/", ("/a/b", "/a"))],
)
def test_normalizes_path(uri, expected):
    assert normalize_path(uri) == expected


def test_rejects_missing_required_fields():
    with pytest.raises(ParseError):
        parse_line("{}", b"k" * 32)