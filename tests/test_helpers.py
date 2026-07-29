"""Tests for the pure module-level helpers."""

from __future__ import annotations

import base64
import json

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


# --------------------------------------------------------------------------
# parse_attachments
# --------------------------------------------------------------------------

HELLO_B64 = base64.b64encode(b"hello").decode()


@pytest.mark.parametrize("raw", ["", "   ", None, "[]"])
def test_parse_attachments_empty(raw):
    assert m.parse_attachments(raw) == []


def test_parse_attachments_reads_a_json_array():
    specs = m.parse_attachments(json.dumps([{"filename": "a.txt", "content_base64": HELLO_B64}]))
    assert specs == [{"filename": "a.txt", "content_base64": HELLO_B64, "content_type": ""}]


def test_parse_attachments_accepts_a_single_object():
    specs = m.parse_attachments(json.dumps({"filename": "a.txt", "content_base64": HELLO_B64}))
    assert len(specs) == 1
    assert specs[0]["filename"] == "a.txt"


def test_parse_attachments_accepts_an_already_decoded_list():
    specs = m.parse_attachments([{"filename": "a.txt", "content_base64": HELLO_B64}])
    assert specs[0]["content_base64"] == HELLO_B64


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        ({"name": "a.txt", "content": HELLO_B64, "type": "text/plain"}, "text/plain"),
        ({"file_name": "a.txt", "data": HELLO_B64, "mime_type": "text/plain"}, "text/plain"),
        ({"FileName": "a.txt", "Base64": HELLO_B64, "MimeType": "text/plain"}, "text/plain"),
    ],
)
def test_parse_attachments_understands_key_aliases(payload, expected_type):
    specs = m.parse_attachments([payload])
    assert specs[0] == {"filename": "a.txt", "content_base64": HELLO_B64, "content_type": expected_type}


def test_parse_attachments_rejects_broken_json():
    with pytest.raises(m.AttachmentError, match="not valid JSON"):
        m.parse_attachments("[{filename: a.txt}")


def test_parse_attachments_rejects_a_bare_scalar():
    with pytest.raises(m.AttachmentError):
        m.parse_attachments("42")


def test_parse_attachments_rejects_non_objects_in_the_array():
    with pytest.raises(m.AttachmentError, match="attachment 1"):
        m.parse_attachments('["a.txt"]')


def test_parse_attachments_requires_content():
    with pytest.raises(m.AttachmentError, match="no base64 content"):
        m.parse_attachments([{"filename": "a.txt"}])


# --------------------------------------------------------------------------
# decode_attachment_content
# --------------------------------------------------------------------------


def test_decode_attachment_content_plain():
    assert m.decode_attachment_content(HELLO_B64, "a.txt") == (b"hello", "")


def test_decode_attachment_content_ignores_whitespace_and_newlines():
    wrapped = "  aGVs\nbG8=  \t"
    assert m.decode_attachment_content(wrapped, "a.txt") == (b"hello", "")


def test_decode_attachment_content_strips_a_data_uri_and_keeps_its_type():
    content, hint = m.decode_attachment_content(f"data:text/plain;base64,{HELLO_B64}", "a.txt")
    assert content == b"hello"
    assert hint == "text/plain"


def test_decode_attachment_content_accepts_the_url_safe_alphabet():
    raw = base64.urlsafe_b64encode(b"\xfb\xef\xbe").decode()
    assert "-" in raw or "_" in raw
    assert m.decode_attachment_content(raw, "a.bin")[0] == b"\xfb\xef\xbe"


def test_decode_attachment_content_restores_missing_padding():
    assert m.decode_attachment_content(HELLO_B64.rstrip("="), "a.txt")[0] == b"hello"


@pytest.mark.parametrize("raw", ["not base64 at all!", "****", "a==="])
def test_decode_attachment_content_rejects_unusable_input(raw):
    with pytest.raises(m.AttachmentError, match="not valid base64"):
        m.decode_attachment_content(raw, "a.txt")


@pytest.mark.parametrize("raw", ["", "   ", None, "data:text/plain;base64,"])
def test_decode_attachment_content_rejects_missing_content(raw):
    with pytest.raises(m.AttachmentError, match="no base64 content"):
        m.decode_attachment_content(raw, "a.txt")


def test_decode_attachment_content_rejects_a_truncated_string():
    # 5 characters can never be padded to a whole number of base64 quanta.
    with pytest.raises(m.AttachmentError, match="truncated"):
        m.decode_attachment_content("aGVsb", "a.txt")


# --------------------------------------------------------------------------
# sanitize_filename / resolve_content_type / format_size
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\temp\\notes.txt", "notes.txt"),
        ("/absolute/path/x.png", "x.png"),
        ('"quoted.txt"', "quoted.txt"),
        ("bad\nname.txt", "badname.txt"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert m.sanitize_filename(raw, 1)[0] == expected


def test_sanitize_filename_notes_a_change():
    name, note = m.sanitize_filename("../secret.txt", 2)
    assert name == "secret.txt"
    assert "attachment 2" in note


@pytest.mark.parametrize("raw", ["", "   ", "/", "...", "\x00"])
def test_sanitize_filename_generates_a_name_when_nothing_is_left(raw):
    name, note = m.sanitize_filename(raw, 3)
    assert name == "attachment-3"
    assert note


def test_sanitize_filename_shortens_and_keeps_the_extension():
    name, note = m.sanitize_filename("x" * 400 + ".pdf", 1)
    assert len(name) <= m.MAX_ATTACHMENT_FILENAME_LENGTH
    assert name.endswith(".pdf")
    assert "shortened" in note


@pytest.mark.parametrize(
    ("declared", "filename", "hint", "expected"),
    [
        ("application/pdf", "x.bin", "", "application/pdf"),
        ("", "report.pdf", "", "application/pdf"),
        ("", "x.bin", "image/png", "image/png"),
        ("pdf", "x.unknownext", "", "application/octet-stream"),
        ("", "x.unknownext", "", "application/octet-stream"),
        ("APPLICATION/PDF", "x.bin", "", "application/pdf"),
    ],
)
def test_resolve_content_type(declared, filename, hint, expected):
    assert m.resolve_content_type(declared, filename, hint) == expected


@pytest.mark.parametrize(
    ("num_bytes", "expected"), [(0, "0 B"), (512, "512 B"), (2048, "2.0 KB"), (3 * 1024 * 1024, "3.0 MB")]
)
def test_format_size(num_bytes, expected):
    assert m.format_size(num_bytes) == expected


def test_format_attachments_never_shows_content():
    rendered = m.format_attachments(
        [{"filename": "a.txt", "content": b"secret-bytes", "content_type": "text/plain", "size": 12}]
    )
    assert rendered == "a.txt (12 B, text/plain)"
    assert "secret-bytes" not in rendered


def test_format_attachments_without_attachments():
    assert m.format_attachments([]) == "(none)"


# --------------------------------------------------------------------------
# build_attachments
# --------------------------------------------------------------------------


def test_build_attachments_decodes_and_describes():
    items, notes = m.build_attachments(
        [{"filename": "a.txt", "content_base64": HELLO_B64, "content_type": ""}], _valves()
    )
    assert notes == []
    assert items == [{"filename": "a.txt", "content": b"hello", "content_type": "text/plain", "size": 5}]


def test_build_attachments_is_a_noop_for_nothing():
    assert m.build_attachments([], _valves()) == ([], [])


def test_build_attachments_rejects_too_many():
    specs = [{"filename": f"{i}.txt", "content_base64": HELLO_B64} for i in range(4)]
    with pytest.raises(m.AttachmentError, match="exceeds the configured limit"):
        m.build_attachments(specs, _valves(max_attachments=3))


def test_build_attachments_rejects_an_oversized_file():
    payload = base64.b64encode(b"x" * (2 * m.BYTES_PER_MB)).decode()
    with pytest.raises(m.AttachmentError, match="per-file limit"):
        m.build_attachments([{"filename": "big.bin", "content_base64": payload}], _valves(max_attachment_size_mb=1))


def test_build_attachments_rejects_an_oversized_total():
    payload = base64.b64encode(b"x" * m.BYTES_PER_MB).decode()
    specs = [{"filename": f"{i}.bin", "content_base64": payload} for i in range(3)]
    with pytest.raises(m.AttachmentError, match="combined limit"):
        m.build_attachments(specs, _valves(max_attachment_size_mb=1, max_total_attachment_size_mb=2))


@pytest.mark.parametrize("filename", ["payload.exe", "PAYLOAD.EXE", "setup.MSI"])
def test_build_attachments_rejects_blocked_extensions(filename):
    with pytest.raises(m.AttachmentError, match="blocked file extension"):
        m.build_attachments([{"filename": filename, "content_base64": HELLO_B64}], _valves())


def test_build_attachments_accepts_a_dotted_blocklist_entry():
    with pytest.raises(m.AttachmentError, match="blocked file extension"):
        m.build_attachments(
            [{"filename": "notes.txt", "content_base64": HELLO_B64}],
            _valves(blocked_attachment_extensions=".txt, .md"),
        )


def test_build_attachments_allows_everything_with_an_empty_blocklist():
    items, _ = m.build_attachments(
        [{"filename": "payload.exe", "content_base64": HELLO_B64}],
        _valves(blocked_attachment_extensions=""),
    )
    assert items[0]["filename"] == "payload.exe"


def test_build_attachments_notes_a_sanitized_filename():
    items, notes = m.build_attachments([{"filename": "../../etc/passwd", "content_base64": HELLO_B64}], _valves())
    assert items[0]["filename"] == "passwd"
    assert any("passwd" in note for note in notes)


def test_build_attachments_keeps_order():
    specs = [{"filename": f"{i}.txt", "content_base64": HELLO_B64} for i in range(3)]
    items, _ = m.build_attachments(specs, _valves())
    assert [i["filename"] for i in items] == ["0.txt", "1.txt", "2.txt"]
