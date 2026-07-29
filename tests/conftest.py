"""Shared fixtures and fakes.

The exchangelib symbols are bound at module level in exchange_email_tool, so the
fakes here simply replace those module attributes - no sys.modules surgery is
needed for the normal paths.
"""

from __future__ import annotations

import pytest

import exchange_email_tool as tool_module

PASSWORD = "sup3r-s3cret-pw"


class FakeInbox:
    name = "Inbox"


class FakeProtocol:
    service_endpoint = "https://mail.example.com/EWS/Exchange.asmx"


class FakeVersion:
    fullname = "Microsoft Exchange Server 2019"


class FakeAccount:
    """Records construction and stands in for a real mailbox connection."""

    instances: list[FakeAccount] = []
    raise_on_init: BaseException | None = None

    def __init__(self, **kwargs):
        if FakeAccount.raise_on_init is not None:
            raise FakeAccount.raise_on_init
        self.kwargs = kwargs
        self.inbox = FakeInbox()
        self.protocol = FakeProtocol()
        self.version = FakeVersion()
        self.primary_smtp_address = kwargs.get("primary_smtp_address", "")
        FakeAccount.instances.append(self)


class FakeMessage:
    """Records construction and send() calls."""

    instances: list[FakeMessage] = []
    raise_on_send: BaseException | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.reply_to = None
        self.sent_with: dict | None = None
        FakeMessage.instances.append(self)

    def send(self, **kwargs):
        if FakeMessage.raise_on_send is not None:
            raise FakeMessage.raise_on_send
        self.sent_with = kwargs


@pytest.fixture
def fake_exchange(monkeypatch):
    """Replace Account and Message with recording fakes."""
    FakeAccount.instances = []
    FakeAccount.raise_on_init = None
    FakeMessage.instances = []
    FakeMessage.raise_on_send = None

    monkeypatch.setattr(tool_module, "Account", FakeAccount)
    monkeypatch.setattr(tool_module, "Message", FakeMessage)
    # apply_global_settings mutates BaseProtocol class attributes process-wide;
    # neutralise it so tests cannot leak state into each other.
    monkeypatch.setattr(tool_module, "apply_global_settings", lambda valves: [])
    return {"account": FakeAccount, "message": FakeMessage}


@pytest.fixture
def tool():
    t = tool_module.Tools()
    t.valves.ews_server = "mail.example.com"
    t.valves.require_confirmation = False
    t.valves.emit_status = False
    return t


@pytest.fixture
def user_ok():
    return {
        "id": "u1",
        "email": "alice@example.com",
        "name": "Alice",
        "role": "user",
        "valves": tool_module.Tools.UserValves(
            username="EXAMPLE\\alice",
            email_address="alice@example.com",
            password=PASSWORD,
        ),
    }


@pytest.fixture
def recording_emitter():
    events: list[dict] = []

    async def emitter(payload):
        events.append(payload)

    emitter.events = events
    return emitter


@pytest.fixture
def confirming_call():
    calls: list[dict] = []

    async def call(payload):
        calls.append(payload)
        return True

    call.calls = calls
    return call


@pytest.fixture
def denying_call():
    calls: list[dict] = []

    async def call(payload):
        calls.append(payload)
        return False

    call.calls = calls
    return call


@pytest.fixture
def erroring_call():
    """Mimics a timed-out or disconnected client: a truthy error dict."""
    calls: list[dict] = []

    async def call(payload):
        calls.append(payload)
        return {"error": "Event call timed out."}

    call.calls = calls
    return call
