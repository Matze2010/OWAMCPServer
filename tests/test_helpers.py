"""Tests for the pure module-level helpers."""

from __future__ import annotations

import pytest

import exchange_email_tool as m

# --------------------------------------------------------------------------
# parse_recipients
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        (None, []),
        ("a@x.de", ["a@x.de"]),
        ("a@x.de, b@y.de", ["a@x.de", "b@y.de"]),
        ("a@x.de; b@y.de", ["a@x.de", "b@y.de"]),
        ("a@x.de\nb@y.de", ["a@x.de", "b@y.de"]),
        ("  a@x.de ,, ; b@y.de  ", ["a@x.de", "b@y.de"]),
        ("Alice <a@x.de>", ["a@x.de"]),
        ("Alice <a@x.de>, Bob <b@y.de>", ["a@x.de", "b@y.de"]),
        (["a@x.de", "b@y.de"], ["a@x.de", "b@y.de"]),
        (("a@x.de, b@y.de",), ["a@x.de", "b@y.de"]),
    ],
)
def test_parse_recipients(raw, expected):
    assert m.parse_recipients(raw) == expected


def test_parse_recipients_dedups_case_insensitively_keeping_first_casing():
    assert m.parse_recipients("Alice@X.de, alice@x.de, B@y.de") == ["Alice@X.de", "B@y.de"]


# --------------------------------------------------------------------------
# is_valid_email
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["a@b.de", "first.last@sub.example.com", "user+tag@example.co.uk", "a_b-c@example-corp.com"],
)
def test_is_valid_email_accepts(address):
    assert m.is_valid_email(address)


@pytest.mark.parametrize(
    "address",
    ["", "john.doe", "john@company", "a@@b.de", "a b@c.de", "a@b.de, c@d.de", "@b.de", "a@.de", "a@b."],
)
def test_is_valid_email_rejects(address):
    assert not m.is_valid_email(address)


def test_is_valid_email_rejects_overlong_address():
    assert not m.is_valid_email("a" * 250 + "@example.com")


# --------------------------------------------------------------------------
# check_domain_policy
# --------------------------------------------------------------------------


def test_domain_policy_allows_everything_when_unset():
    assert m.check_domain_policy(["a@anywhere.com"], "", "") == ([], "")


def test_domain_policy_allow_list_hit():
    assert m.check_domain_policy(["a@example.com"], "example.com", "") == ([], "")


def test_domain_policy_allow_list_miss():
    rejected, reason = m.check_domain_policy(["a@other.com"], "example.com", "")
    assert rejected == ["a@other.com"]
    assert "allow list" in reason


def test_domain_policy_block_list_beats_allow_list():
    rejected, reason = m.check_domain_policy(["a@example.com"], "example.com", "example.com")
    assert rejected == ["a@example.com"]
    assert "block list" in reason


def test_domain_policy_is_case_insensitive():
    assert m.check_domain_policy(["a@EXAMPLE.com"], "example.COM", "") == ([], "")


def test_domain_policy_does_not_expand_subdomains():
    """An allow-list entry must not silently permit subdomains or lookalikes."""
    rejected, _ = m.check_domain_policy(
        ["a@mail.example.com", "b@evil-example.com", "c@example.com"],
        "example.com",
        "",
    )
    assert rejected == ["a@mail.example.com", "b@evil-example.com"]


# --------------------------------------------------------------------------
# looks_like_html / sanitize_html
# --------------------------------------------------------------------------


@pytest.mark.parametrize("body", ["<p>hi</p>", "<html><body>x</body></html>", "line<br>break", '<a href="x">y</a>'])
def test_looks_like_html_positive(body):
    assert m.looks_like_html(body)


@pytest.mark.parametrize("body", ["", "plain text", "a < b and c > d", "5 < 10", "use <- arrows"])
def test_looks_like_html_negative(body):
    assert not m.looks_like_html(body)


def test_sanitize_html_strips_active_content():
    dirty = '<p onclick="steal()">hi</p><script>bad()</script><iframe src="x"></iframe>'
    clean = m.sanitize_html(dirty)
    assert "script" not in clean.lower()
    assert "iframe" not in clean.lower()
    assert "onclick" not in clean.lower()
    assert "<p>hi</p>" in clean


def test_sanitize_html_neutralises_javascript_urls():
    clean = m.sanitize_html('<a href="javascript:alert(1)">x</a>')
    assert "javascript:" not in clean.lower()


# --------------------------------------------------------------------------
# normalise_importance / build_version
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"), [("high", "High"), ("HIGH", "High"), ("low", "Low"), ("Normal", "Normal")]
)
def test_normalise_importance_known(raw, expected):
    value, note = m.normalise_importance(raw)
    assert value == expected
    assert note == ""


def test_normalise_importance_unknown_falls_back_visibly():
    value, note = m.normalise_importance("URGENT")
    assert value == "Normal"
    assert "URGENT" in note


def test_normalise_importance_empty():
    assert m.normalise_importance("") == ("Normal", "")


def test_build_version_parses_build_string():
    version = m.build_version("15.1.2507.16")
    assert version is not None
    assert version.build.major_version == 15


@pytest.mark.parametrize("raw", ["", "   ", "abc", "not.a.build", "15"])
def test_build_version_returns_none_without_raising(raw):
    assert m.build_version(raw) is None


# --------------------------------------------------------------------------
# redact
# --------------------------------------------------------------------------


def test_redact_removes_secret():
    assert m.redact("login failed for hunter2222", ("hunter2222",)) == "login failed for ***"


def test_redact_replaces_every_occurrence():
    assert m.redact("aXXXXb XXXX", ("XXXX",)) == "a***b ***"


@pytest.mark.parametrize("secret", ["", "ab", "abc"])
def test_redact_is_a_noop_for_empty_or_short_secrets(secret):
    """str.replace('', '***') would shred the message - worse than the leak."""
    text = "nothing to hide here"
    assert m.redact(text, (secret,)) == text


def test_redact_accepts_a_bare_string():
    assert m.redact("say hunter2222", "hunter2222") == "say ***"


# --------------------------------------------------------------------------
# resolve_endpoint
# --------------------------------------------------------------------------


def _valves(**kwargs):
    valves = m.Tools.Valves()
    for key, value in kwargs.items():
        setattr(valves, key, value)
    return valves


def test_resolve_endpoint_prefers_service_endpoint():
    kwargs, label = m.resolve_endpoint(
        _valves(ews_server="mail.example.com", ews_service_endpoint="https://x/EWS/Exchange.asmx")
    )
    assert kwargs == {"service_endpoint": "https://x/EWS/Exchange.asmx"}
    assert label == "https://x/EWS/Exchange.asmx"


def test_resolve_endpoint_falls_back_to_server():
    kwargs, label = m.resolve_endpoint(_valves(ews_server="mail.example.com"))
    assert kwargs == {"server": "mail.example.com"}
    assert label == "https://mail.example.com/EWS/Exchange.asmx"


def test_resolve_endpoint_never_returns_both_keys():
    """exchangelib raises AttributeError when both are passed to Configuration."""
    for valves in (
        _valves(ews_server="a", ews_service_endpoint="https://b/EWS/Exchange.asmx"),
        _valves(ews_server="a"),
        _valves(ews_service_endpoint="https://b/EWS/Exchange.asmx"),
        _valves(),
    ):
        kwargs, _ = m.resolve_endpoint(valves)
        assert not ("server" in kwargs and "service_endpoint" in kwargs)


def test_resolve_endpoint_empty_for_autodiscover():
    kwargs, label = m.resolve_endpoint(_valves())
    assert kwargs == {}
    assert "autodiscover" in label


# --------------------------------------------------------------------------
# resolve_user_valves
# --------------------------------------------------------------------------


def test_resolve_user_valves_without_user():
    valves = m.resolve_user_valves(None, m.Tools.UserValves)
    assert valves.username == ""


def test_resolve_user_valves_without_valves_key():
    valves = m.resolve_user_valves({"id": "u1"}, m.Tools.UserValves)
    assert valves.username == ""


def test_resolve_user_valves_from_model():
    original = m.Tools.UserValves(username="DOM\\bob")
    assert m.resolve_user_valves({"valves": original}, m.Tools.UserValves) is original


def test_resolve_user_valves_from_plain_dict():
    valves = m.resolve_user_valves({"valves": {"username": "DOM\\bob"}}, m.Tools.UserValves)
    assert valves.username == "DOM\\bob"


def test_resolve_user_valves_from_unusable_value():
    assert m.resolve_user_valves({"valves": 42}, m.Tools.UserValves).username == ""


# --------------------------------------------------------------------------
# Valve schema as Open WebUI reads it
# --------------------------------------------------------------------------


def test_password_valve_is_declared_as_a_password_input():
    """Open WebUI checks properties[field].input.type == 'password' to render
    its masked SensitiveInput, so the hint must survive in the JSON schema."""
    schema = m.Tools.UserValves.model_json_schema()
    assert schema["properties"]["password"]["input"] == {"type": "password"}


def test_only_the_password_valve_is_masked():
    for model in (m.Tools.Valves, m.Tools.UserValves):
        for name, spec in model.model_json_schema()["properties"].items():
            if name != "password":
                assert "input" not in spec, name


def test_every_valve_has_a_description_for_the_admin_ui():
    for model in (m.Tools.Valves, m.Tools.UserValves):
        for name, spec in model.model_json_schema()["properties"].items():
            assert spec.get("description"), name


# --------------------------------------------------------------------------
# append_signature
# --------------------------------------------------------------------------


def test_append_signature_plain():
    assert m.append_signature("Hello", "Alice\nExample Ltd", as_html=False) == "Hello\n\n-- \nAlice\nExample Ltd"


def test_append_signature_html_escapes_and_breaks():
    result = m.append_signature("<p>Hello</p>", "Alice & Co\nExample", as_html=True)
    assert "Alice &amp; Co<br>Example" in result
    assert result.startswith("<p>Hello</p><br><br>-- <br>")


def test_append_signature_noop_when_empty():
    assert m.append_signature("Hello", "   ", as_html=False) == "Hello"


# --------------------------------------------------------------------------
# describe_error
# --------------------------------------------------------------------------


def test_error_map_is_fully_resolved_against_installed_exchangelib():
    assert all(cls is not None for cls, _ in m.ERROR_MAP)


def test_describe_error_prefers_specific_over_transport():
    """ResponseMessageError sits below TransportError - ordering must not swallow it."""
    from exchangelib.errors import ErrorAccessDenied, TransportError

    specific = m.describe_error(ErrorAccessDenied("nope"))
    generic = m.describe_error(TransportError("nope"))
    assert specific != generic
    assert "denied access" in specific


def test_describe_error_falls_back_for_unknown_exception():
    assert "unexpected" in m.describe_error(RuntimeError("boom")).lower()
