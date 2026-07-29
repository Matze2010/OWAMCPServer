"""Tests for Tools.send_email: validation, dry run, confirmation, sending, errors."""

from __future__ import annotations

import base64
import importlib
import json
import sys

import pytest
from conftest import PASSWORD, FakeAccount, FakeMessage

import exchange_email_tool as m


async def send(tool, user, **overrides):
    kwargs = {
        "to": "bob@example.com",
        "subject": "Quarterly report",
        "body": "Please find the numbers attached.",
        "__user__": user,
    }
    kwargs.update(overrides)
    return await tool.send_email(**kwargs)


def attachment_arg(*specs):
    """Build the JSON the model would emit. Each spec is (filename, content[, type])."""
    payload = []
    for spec in specs or (("notes.txt", b"hello"),):
        filename, content = spec[0], spec[1]
        item = {"filename": filename, "content_base64": base64.b64encode(content).decode()}
        if len(spec) > 2:
            item["content_type"] = spec[2]
        payload.append(item)
    return json.dumps(payload)


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------


async def test_dry_run_makes_no_network_calls(tool, user_ok, fake_exchange, confirming_call):
    tool.valves.dry_run = True
    tool.valves.require_confirmation = True

    result = await send(tool, user_ok, cc="carl@example.com", __event_call__=confirming_call)

    assert result.startswith(m.BANNER_DRY_RUN)
    assert result.rstrip().endswith("Disable the 'dry_run' valve to send for real.")
    assert m.BANNER_DRY_RUN in result.splitlines()[-1]
    assert "bob@example.com" in result
    assert "carl@example.com" in result
    assert "Quarterly report" in result
    assert FakeAccount.instances == []
    assert FakeMessage.instances == []
    # A dry run must not even ask for confirmation - nothing can be sent.
    assert confirming_call.calls == []


async def test_dry_run_still_enforces_domain_policy(tool, user_ok, fake_exchange):
    tool.valves.dry_run = True
    tool.valves.blocked_recipient_domains = "example.com"

    result = await send(tool, user_ok)

    assert result.startswith(m.BANNER_NOT_SENT)
    assert "block list" in result


async def test_dry_run_shows_body_preview(tool, user_ok, fake_exchange):
    tool.valves.dry_run = True
    result = await send(tool, user_ok, body="x" * 800)
    assert "[…]" in result


# --------------------------------------------------------------------------
# Confirmation
# --------------------------------------------------------------------------


async def test_confirmed_send_goes_through(tool, user_ok, fake_exchange, confirming_call):
    tool.valves.require_confirmation = True

    result = await send(tool, user_ok, __event_call__=confirming_call)

    assert result.startswith(m.BANNER_SENT)
    assert len(FakeMessage.instances) == 1
    assert FakeMessage.instances[0].sent_with is not None
    assert confirming_call.calls[0]["type"] == "confirmation"


async def test_denied_confirmation_does_not_send(tool, user_ok, fake_exchange, denying_call):
    tool.valves.require_confirmation = True

    result = await send(tool, user_ok, __event_call__=denying_call)

    assert result.startswith(m.BANNER_NOT_SENT)
    assert "not confirmed" in result
    assert FakeMessage.instances == []
    assert FakeAccount.instances == []


async def test_confirmation_error_dict_is_treated_as_refusal(tool, user_ok, fake_exchange, erroring_call):
    """__event_call__ returns a *truthy* {"error": ...} on timeout - must fail closed."""
    tool.valves.require_confirmation = True

    result = await send(tool, user_ok, __event_call__=erroring_call)

    assert result.startswith(m.BANNER_NOT_SENT)
    assert FakeMessage.instances == []


async def test_confirmation_exception_is_treated_as_refusal(tool, user_ok, fake_exchange):
    tool.valves.require_confirmation = True

    async def exploding_call(payload):
        raise RuntimeError("socket gone")

    result = await send(tool, user_ok, __event_call__=exploding_call)

    assert result.startswith(m.BANNER_NOT_SENT)
    assert FakeMessage.instances == []


async def test_missing_event_call_blocks_sending(tool, user_ok, fake_exchange):
    tool.valves.require_confirmation = True

    result = await send(tool, user_ok, __event_call__=None)

    assert result.startswith(m.BANNER_NOT_SENT)
    assert "require_confirmation" in result
    assert FakeMessage.instances == []


async def test_confirmation_can_be_disabled(tool, user_ok, fake_exchange):
    tool.valves.require_confirmation = False

    result = await send(tool, user_ok)

    assert result.startswith(m.BANNER_SENT)


async def test_confirmation_dict_shape_is_accepted(tool, user_ok, fake_exchange):
    tool.valves.require_confirmation = True

    async def call(payload):
        return {"confirmed": True}

    result = await send(tool, user_ok, __event_call__=call)
    assert result.startswith(m.BANNER_SENT)


async def test_confirmation_message_shows_details_but_no_password(tool, user_ok, fake_exchange, confirming_call):
    tool.valves.require_confirmation = True

    await send(tool, user_ok, cc="carl@example.com", bcc="dora@example.com", __event_call__=confirming_call)

    message = confirming_call.calls[0]["data"]["message"]
    assert "bob@example.com" in message
    assert "carl@example.com" in message
    assert "Quarterly report" in message
    assert "1 recipient(s)" in message  # Bcc as a count only
    assert "dora@example.com" not in message
    assert PASSWORD not in message


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


async def test_disabled_user(tool, user_ok, fake_exchange):
    user_ok["valves"].enabled = False
    result = await send(tool, user_ok)
    assert result.startswith(m.BANNER_NOT_SENT)
    assert "disabled" in result
    assert FakeAccount.instances == []


@pytest.mark.parametrize("field", ["username", "email_address", "password"])
async def test_missing_credentials_are_named(tool, user_ok, fake_exchange, field):
    setattr(user_ok["valves"], field, "")
    result = await send(tool, user_ok)
    assert result.startswith(m.BANNER_NOT_SENT)
    assert field in result
    assert FakeAccount.instances == []


async def test_invalid_sender_address(tool, user_ok, fake_exchange):
    user_ok["valves"].email_address = "alice"
    result = await send(tool, user_ok)
    assert "not a valid email address" in result


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"to": ""}, "no recipient"),
        ({"to": "not-an-address"}, "not valid"),
        ({"subject": "   "}, "subject must not be empty"),
        ({"body": "   "}, "body must not be empty"),
    ],
)
async def test_validation_failures(tool, user_ok, fake_exchange, overrides, expected):
    result = await send(tool, user_ok, **overrides)
    assert result.startswith(m.BANNER_NOT_SENT)
    assert expected in result
    assert FakeMessage.instances == []


async def test_max_recipients_rejects_whole_message(tool, user_ok, fake_exchange):
    tool.valves.max_recipients = 2
    result = await send(tool, user_ok, to="a@example.com, b@example.com", cc="c@example.com")
    assert result.startswith(m.BANNER_NOT_SENT)
    assert "exceeds the configured limit of 2" in result
    assert FakeMessage.instances == []


async def test_blocked_domain(tool, user_ok, fake_exchange):
    tool.valves.blocked_recipient_domains = "spam.test"
    result = await send(tool, user_ok, to="bob@example.com, evil@spam.test")
    assert "evil@spam.test" in result
    assert FakeMessage.instances == []


async def test_allow_list_miss(tool, user_ok, fake_exchange):
    tool.valves.allowed_recipient_domains = "example.com"
    result = await send(tool, user_ok, to="bob@other.com")
    assert "allow list" in result
    assert FakeMessage.instances == []


async def test_all_invalid_addresses_reported_at_once(tool, user_ok, fake_exchange):
    result = await send(tool, user_ok, to="bad1, bad2, ok@example.com")
    assert "bad1" in result and "bad2" in result


# --------------------------------------------------------------------------
# Send path
# --------------------------------------------------------------------------


async def test_message_fields_are_passed_through(tool, user_ok, fake_exchange):
    await send(
        tool,
        user_ok,
        to="a@example.com, b@example.com",
        cc="c@example.com",
        bcc="d@example.com",
        reply_to="e@example.com",
        importance="high",
    )

    message = FakeMessage.instances[0]
    assert message.kwargs["to_recipients"] == ["a@example.com", "b@example.com"]
    assert message.kwargs["cc_recipients"] == ["c@example.com"]
    assert message.kwargs["bcc_recipients"] == ["d@example.com"]
    assert message.kwargs["subject"] == "Quarterly report"
    assert message.kwargs["importance"] == "High"
    assert message.reply_to == ["e@example.com"]


async def test_account_uses_delegate_access(tool, user_ok, fake_exchange):
    await send(tool, user_ok)
    account = FakeAccount.instances[0]
    assert account.kwargs["access_type"] == m.DELEGATE
    assert account.kwargs["autodiscover"] is False
    assert account.kwargs["primary_smtp_address"] == "alice@example.com"


async def test_configuration_never_gets_server_and_endpoint_together(tool, user_ok, fake_exchange):
    tool.valves.ews_server = "mail.example.com"
    tool.valves.ews_service_endpoint = "https://other.example.com/EWS/Exchange.asmx"

    await send(tool, user_ok)

    config = FakeAccount.instances[0].kwargs["config"]
    assert config.service_endpoint == "https://other.example.com/EWS/Exchange.asmx"


@pytest.mark.parametrize(("save", "expected"), [(True, True), (False, False)])
async def test_save_to_sent_items_maps_to_save_copy(tool, user_ok, fake_exchange, save, expected):
    tool.valves.save_to_sent_items = save
    await send(tool, user_ok)
    assert FakeMessage.instances[0].sent_with == {"save_copy": expected}


async def test_plain_body_is_not_wrapped_in_html(tool, user_ok, fake_exchange):
    await send(tool, user_ok, body="Just text")
    assert not isinstance(FakeMessage.instances[0].kwargs["body"], m.HTMLBody)


async def test_explicit_html_flag_wraps_body(tool, user_ok, fake_exchange):
    await send(tool, user_ok, body="<p>Hello</p>", body_is_html=True)
    assert isinstance(FakeMessage.instances[0].kwargs["body"], m.HTMLBody)


async def test_auto_detection_upgrades_forgotten_html(tool, user_ok, fake_exchange):
    result = await send(tool, user_ok, body="<p>Hello</p>", body_is_html=False)
    assert isinstance(FakeMessage.instances[0].kwargs["body"], m.HTMLBody)
    assert "detected as HTML" in result


async def test_auto_detection_can_be_disabled(tool, user_ok, fake_exchange):
    tool.valves.auto_detect_html = False
    await send(tool, user_ok, body="<p>Hello</p>", body_is_html=False)
    assert not isinstance(FakeMessage.instances[0].kwargs["body"], m.HTMLBody)


async def test_html_body_is_sanitized(tool, user_ok, fake_exchange):
    await send(tool, user_ok, body="<p>hi</p><script>bad()</script>", body_is_html=True)
    assert "script" not in str(FakeMessage.instances[0].kwargs["body"]).lower()


async def test_signature_is_appended_to_plain_body(tool, user_ok, fake_exchange):
    user_ok["valves"].signature = "Alice\nExample Ltd"
    await send(tool, user_ok, body="Hello")
    assert str(FakeMessage.instances[0].kwargs["body"]) == "Hello\n\n-- \nAlice\nExample Ltd"


async def test_signature_is_escaped_in_html_body(tool, user_ok, fake_exchange):
    user_ok["valves"].signature = "Alice & Co"
    await send(tool, user_ok, body="<p>Hello</p>", body_is_html=True)
    assert "Alice &amp; Co" in str(FakeMessage.instances[0].kwargs["body"])


async def test_success_block_reports_bcc_as_count_only(tool, user_ok, fake_exchange):
    result = await send(tool, user_ok, bcc="secret@example.com")
    assert "secret@example.com" not in result
    assert "1 recipient(s)" in result


async def test_unknown_importance_is_reported(tool, user_ok, fake_exchange):
    result = await send(tool, user_ok, importance="urgent")
    assert "urgent" in result
    assert FakeMessage.instances[0].kwargs["importance"] == "Normal"


# --------------------------------------------------------------------------
# Error mapping
# --------------------------------------------------------------------------


def _exchange_errors():
    from exchangelib.errors import ErrorAccessDenied, ErrorNonExistentMailbox, TransportError, UnauthorizedError

    return [
        (UnauthorizedError("nope"), "Authentication failed"),
        (ErrorNonExistentMailbox("nope"), "No mailbox exists"),
        (ErrorAccessDenied("nope"), "denied access"),
        (TransportError("nope"), "Could not communicate"),
    ]


@pytest.mark.parametrize(("exc", "expected"), _exchange_errors())
async def test_exchange_errors_map_to_friendly_messages(tool, user_ok, fake_exchange, exc, expected):
    FakeMessage.raise_on_send = exc

    result = await send(tool, user_ok)

    assert result.startswith(m.BANNER_NOT_SENT)
    assert expected in result
    assert PASSWORD not in result


async def test_ssl_error_points_at_the_certificate_valves(tool, user_ok, fake_exchange):
    import requests.exceptions

    FakeAccount.raise_on_init = requests.exceptions.SSLError("bad cert")

    result = await send(tool, user_ok)

    assert "ca_bundle_path" in result


async def test_unexpected_error_is_contained(tool, user_ok, fake_exchange):
    FakeMessage.raise_on_send = RuntimeError("boom")

    result = await send(tool, user_ok)

    assert result.startswith(m.BANNER_NOT_SENT)
    assert "boom" not in result


async def test_debug_errors_appends_technical_detail(tool, user_ok, fake_exchange):
    tool.valves.debug_errors = True
    FakeMessage.raise_on_send = RuntimeError("boom")

    result = await send(tool, user_ok)

    assert "RuntimeError" in result and "boom" in result


async def test_sent_items_hint_when_access_denied(tool, user_ok, fake_exchange):
    from exchangelib.errors import ErrorAccessDenied

    tool.valves.save_to_sent_items = True
    FakeMessage.raise_on_send = ErrorAccessDenied("no folder")

    result = await send(tool, user_ok)

    assert "save_to_sent_items" in result


# --------------------------------------------------------------------------
# Status events
# --------------------------------------------------------------------------


async def test_status_sequence_on_success(tool, user_ok, fake_exchange, recording_emitter):
    tool.valves.emit_status = True

    await send(tool, user_ok, __event_emitter__=recording_emitter)

    descriptions = [e["data"]["description"] for e in recording_emitter.events]
    assert descriptions[0].startswith("Checking configuration")
    assert sum(1 for e in recording_emitter.events if e["data"]["done"]) == 1
    assert recording_emitter.events[-1]["data"]["done"] is True


async def test_status_terminates_on_validation_failure(tool, user_ok, fake_exchange, recording_emitter):
    tool.valves.emit_status = True

    await send(tool, user_ok, to="", __event_emitter__=recording_emitter)

    assert sum(1 for e in recording_emitter.events if e["data"]["done"]) == 1


async def test_status_terminates_on_dry_run(tool, user_ok, fake_exchange, recording_emitter):
    tool.valves.emit_status = True
    tool.valves.dry_run = True

    await send(tool, user_ok, __event_emitter__=recording_emitter)

    assert sum(1 for e in recording_emitter.events if e["data"]["done"]) == 1


async def test_a_throwing_emitter_does_not_break_sending(tool, user_ok, fake_exchange):
    tool.valves.emit_status = True

    async def broken_emitter(payload):
        raise RuntimeError("frontend gone")

    result = await send(tool, user_ok, __event_emitter__=broken_emitter)

    assert result.startswith(m.BANNER_SENT)


# --------------------------------------------------------------------------
# Attachments
# --------------------------------------------------------------------------


async def test_attachment_reaches_the_message(tool, user_ok, fake_exchange):
    await send(tool, user_ok, attachments=attachment_arg(("report.pdf", b"%PDF-1.4", "application/pdf")))

    attachments = FakeMessage.instances[0].attachments
    assert len(attachments) == 1
    assert attachments[0].name == "report.pdf"
    assert attachments[0].content == b"%PDF-1.4"
    assert attachments[0].content_type == "application/pdf"


async def test_content_type_is_derived_from_the_filename(tool, user_ok, fake_exchange):
    await send(tool, user_ok, attachments=attachment_arg(("notes.txt", b"hello")))

    assert FakeMessage.instances[0].attachments[0].content_type == "text/plain"


async def test_several_attachments_keep_their_order(tool, user_ok, fake_exchange):
    await send(
        tool,
        user_ok,
        attachments=attachment_arg(("a.txt", b"one"), ("b.txt", b"two"), ("c.txt", b"three")),
    )

    assert [a.name for a in FakeMessage.instances[0].attachments] == ["a.txt", "b.txt", "c.txt"]


async def test_message_without_attachments_gets_none(tool, user_ok, fake_exchange):
    await send(tool, user_ok)

    assert getattr(FakeMessage.instances[0], "attachments", []) == []


async def test_success_block_reports_attachments_as_a_count(tool, user_ok, fake_exchange):
    secret = b"top-secret-file-content"
    encoded = base64.b64encode(secret).decode()

    result = await send(tool, user_ok, attachments=attachment_arg(("notes.txt", secret), ("more.txt", b"x")))

    assert f"Attachments: 2 file(s), {m.format_size(len(secret) + 1)}" in result
    assert "notes.txt" not in result
    assert encoded not in result
    assert "top-secret-file-content" not in result


async def test_success_block_says_none_without_attachments(tool, user_ok, fake_exchange):
    result = await send(tool, user_ok)

    assert "Attachments: (none)" in result


async def test_dry_run_reports_attachments_as_a_count(tool, user_ok, fake_exchange):
    tool.valves.dry_run = True
    encoded = base64.b64encode(b"hello").decode()

    result = await send(tool, user_ok, attachments=attachment_arg(("notes.txt", b"hello")))

    assert result.startswith(m.BANNER_DRY_RUN)
    assert "Attachments: 1 file(s), 5 B" in result
    assert "notes.txt" not in result
    assert encoded not in result
    assert FakeAccount.instances == []


async def test_confirmation_dialog_reports_attachments_as_a_count(tool, user_ok, fake_exchange, confirming_call):
    tool.valves.require_confirmation = True

    await send(
        tool,
        user_ok,
        attachments=attachment_arg(("notes.txt", b"hello"), ("b.txt", b"hi")),
        __event_call__=confirming_call,
    )

    message = confirming_call.calls[0]["data"]["message"]
    assert "Anhänge: 2 Datei(en), 7 B" in message
    assert "notes.txt" not in message
    assert base64.b64encode(b"hello").decode() not in message


async def test_attachments_can_be_disabled_by_the_administrator(tool, user_ok, fake_exchange):
    tool.valves.allow_attachments = False

    result = await send(tool, user_ok, attachments=attachment_arg())

    assert result.startswith(m.BANNER_NOT_SENT)
    assert "disabled by the administrator" in result
    assert FakeAccount.instances == []


async def test_disabling_attachments_does_not_block_plain_messages(tool, user_ok, fake_exchange):
    tool.valves.allow_attachments = False

    result = await send(tool, user_ok)

    assert result.startswith(m.BANNER_SENT)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"attachments": "[{filename: nope}"}, "not valid JSON"),
        ({"attachments": '[{"filename": "a.txt", "content_base64": "!!!not base64!!!"}]'}, "not valid base64"),
        ({"attachments": '[{"filename": "a.txt"}]'}, "no base64 content"),
    ],
)
async def test_unusable_attachments_are_rejected(tool, user_ok, fake_exchange, overrides, expected):
    result = await send(tool, user_ok, **overrides)

    assert result.startswith(m.BANNER_NOT_SENT)
    assert expected in result
    assert FakeAccount.instances == []


async def test_too_many_attachments_rejects_the_whole_message(tool, user_ok, fake_exchange):
    tool.valves.max_attachments = 2
    payload = attachment_arg(("a.txt", b"a"), ("b.txt", b"b"), ("c.txt", b"c"))

    result = await send(tool, user_ok, attachments=payload)

    assert result.startswith(m.BANNER_NOT_SENT)
    assert "exceeds the configured limit of 2" in result
    assert FakeAccount.instances == []


async def test_oversized_attachment_rejects_the_whole_message(tool, user_ok, fake_exchange):
    tool.valves.max_attachment_size_mb = 1

    result = await send(tool, user_ok, attachments=attachment_arg(("big.bin", b"x" * (2 * m.BYTES_PER_MB))))

    assert result.startswith(m.BANNER_NOT_SENT)
    assert "per-file limit" in result
    assert FakeAccount.instances == []


async def test_blocked_extension_rejects_the_whole_message(tool, user_ok, fake_exchange):
    result = await send(tool, user_ok, attachments=attachment_arg(("payload.exe", b"MZ")))

    assert result.startswith(m.BANNER_NOT_SENT)
    assert "blocked file extension" in result
    assert FakeAccount.instances == []


async def test_sanitized_filename_is_reported_and_used(tool, user_ok, fake_exchange):
    result = await send(tool, user_ok, attachments=attachment_arg(("../../etc/passwd", b"root:x:0:0")))

    assert result.startswith(m.BANNER_SENT)
    assert FakeMessage.instances[0].attachments[0].name == "passwd"
    assert "Note:" in result


async def test_status_events_terminate_once_with_attachments(tool, user_ok, fake_exchange, recording_emitter):
    tool.valves.emit_status = True

    await send(tool, user_ok, attachments=attachment_arg(), __event_emitter__=recording_emitter)

    done_events = [e for e in recording_emitter.events if e["data"]["done"]]
    assert len(done_events) == 1


async def test_status_events_terminate_once_when_an_attachment_is_rejected(
    tool, user_ok, fake_exchange, recording_emitter
):
    tool.valves.emit_status = True

    await send(tool, user_ok, attachments="not json", __event_emitter__=recording_emitter)

    done_events = [e for e in recording_emitter.events if e["data"]["done"]]
    assert len(done_events) == 1


# --------------------------------------------------------------------------
# Password never leaks
# --------------------------------------------------------------------------


@pytest.mark.parametrize("debug_errors", [False, True])
async def test_password_never_appears_in_output(tool, user_ok, fake_exchange, recording_emitter, debug_errors):
    tool.valves.emit_status = True
    tool.valves.debug_errors = debug_errors

    outputs = []

    outputs.append(await send(tool, user_ok, __event_emitter__=recording_emitter))

    tool.valves.dry_run = True
    outputs.append(await send(tool, user_ok, __event_emitter__=recording_emitter))
    tool.valves.dry_run = False

    outputs.append(await send(tool, user_ok, to="", __event_emitter__=recording_emitter))

    outputs.append(await send(tool, user_ok, attachments=attachment_arg(), __event_emitter__=recording_emitter))

    FakeMessage.raise_on_send = RuntimeError(f"auth blew up with {PASSWORD}")
    outputs.append(await send(tool, user_ok, __event_emitter__=recording_emitter))

    for output in outputs:
        assert PASSWORD not in output
    for event in recording_emitter.events:
        assert PASSWORD not in event["data"]["description"]


async def test_attachment_content_never_appears_in_output(tool, user_ok, fake_exchange, recording_emitter):
    tool.valves.emit_status = True
    content = b"confidential-payroll-data"
    encoded = base64.b64encode(content).decode()
    payload = attachment_arg(("payroll.txt", content))

    outputs = [await send(tool, user_ok, attachments=payload, __event_emitter__=recording_emitter)]

    tool.valves.dry_run = True
    outputs.append(await send(tool, user_ok, attachments=payload, __event_emitter__=recording_emitter))

    for output in outputs:
        assert encoded not in output
        assert content.decode() not in output
    for event in recording_emitter.events:
        assert encoded not in event["data"]["description"]


# --------------------------------------------------------------------------
# Missing exchangelib
# --------------------------------------------------------------------------


@pytest.fixture
def module_without_exchangelib(monkeypatch):
    """Reload the tool module with the exchangelib import blocked."""
    monkeypatch.setitem(sys.modules, "exchangelib", None)
    reloaded = importlib.reload(m)
    yield reloaded
    monkeypatch.undo()
    importlib.reload(m)


async def test_module_imports_without_exchangelib(module_without_exchangelib):
    assert module_without_exchangelib.EXCHANGELIB_AVAILABLE is False
    assert module_without_exchangelib.EXCHANGELIB_IMPORT_ERROR
    assert all(cls is None for cls, _ in module_without_exchangelib.ERROR_MAP)


async def test_send_reports_missing_library(module_without_exchangelib, user_ok):
    tool = module_without_exchangelib.Tools()
    tool.valves.ews_server = "mail.example.com"
    tool.valves.require_confirmation = False
    tool.valves.emit_status = False
    user = dict(user_ok)
    user["valves"] = module_without_exchangelib.Tools.UserValves(
        username="EXAMPLE\\alice", email_address="alice@example.com", password=PASSWORD
    )

    result = await tool.send_email(to="bob@example.com", subject="s", body="b", __user__=user)

    assert "exchangelib" in result
    assert result.startswith(module_without_exchangelib.BANNER_NOT_SENT)


async def test_dry_run_still_works_without_exchangelib(module_without_exchangelib, user_ok):
    tool = module_without_exchangelib.Tools()
    tool.valves.dry_run = True
    tool.valves.emit_status = False
    user = dict(user_ok)
    user["valves"] = module_without_exchangelib.Tools.UserValves(
        username="EXAMPLE\\alice", email_address="alice@example.com", password=PASSWORD
    )

    result = await tool.send_email(to="bob@example.com", subject="s", body="b", __user__=user)

    assert result.startswith(module_without_exchangelib.BANNER_DRY_RUN)


async def test_dry_run_with_attachments_works_without_exchangelib(module_without_exchangelib, user_ok):
    """FileAttachment is None here - the dry run must never reach it."""
    tool = module_without_exchangelib.Tools()
    tool.valves.dry_run = True
    tool.valves.emit_status = False
    user = dict(user_ok)
    user["valves"] = module_without_exchangelib.Tools.UserValves(
        username="EXAMPLE\\alice", email_address="alice@example.com", password=PASSWORD
    )

    assert module_without_exchangelib.FileAttachment is None
    result = await tool.send_email(
        to="bob@example.com", subject="s", body="b", attachments=attachment_arg(), __user__=user
    )

    assert result.startswith(module_without_exchangelib.BANNER_DRY_RUN)
    assert "Attachments: 1 file(s), 5 B" in result
