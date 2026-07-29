"""Tests for Tools.check_exchange_connection."""

from __future__ import annotations

import pytest
from conftest import PASSWORD, FakeAccount, FakeMessage

import exchange_email_tool as m


async def test_successful_check_reports_endpoint_and_mailbox(tool, user_ok, fake_exchange):
    result = await tool.check_exchange_connection(__user__=user_ok)

    assert result.startswith("CONNECTION CHECK OK")
    assert "https://mail.example.com/EWS/Exchange.asmx" in result
    assert "alice@example.com" in result
    assert "Microsoft Exchange Server 2019" in result
    assert "NTLM" in result


async def test_check_never_sends_anything(tool, user_ok, fake_exchange):
    await tool.check_exchange_connection(__user__=user_ok)
    assert FakeMessage.instances == []


async def test_check_ignores_dry_run_and_connects_for_real(tool, user_ok, fake_exchange):
    """dry_run must not give the false impression that the check was simulated."""
    tool.valves.dry_run = True

    result = await tool.check_exchange_connection(__user__=user_ok)

    assert result.startswith("CONNECTION CHECK OK")
    assert len(FakeAccount.instances) == 1
    assert "dry_run" in result


async def test_check_requires_credentials(tool, user_ok, fake_exchange):
    user_ok["valves"].password = ""

    result = await tool.check_exchange_connection(__user__=user_ok)

    assert result.startswith("CONNECTION CHECK FAILED")
    assert "password" in result
    assert FakeAccount.instances == []


async def test_check_respects_the_personal_switch(tool, user_ok, fake_exchange):
    user_ok["valves"].enabled = False

    result = await tool.check_exchange_connection(__user__=user_ok)

    assert result.startswith("CONNECTION CHECK FAILED")
    assert FakeAccount.instances == []


async def test_check_without_user_valves_fails_cleanly(tool, fake_exchange):
    result = await tool.check_exchange_connection(__user__=None)

    assert result.startswith("CONNECTION CHECK FAILED")
    assert "username" in result


@pytest.mark.parametrize(
    ("exc_name", "expected"),
    [
        ("UnauthorizedError", "Authentication failed"),
        ("ErrorNonExistentMailbox", "No mailbox exists"),
        ("AutoDiscoverFailed", "Autodiscover"),
        ("TransportError", "Could not communicate"),
    ],
)
async def test_check_maps_errors(tool, user_ok, fake_exchange, exc_name, expected):
    from exchangelib import errors

    FakeAccount.raise_on_init = getattr(errors, exc_name)("nope")

    result = await tool.check_exchange_connection(__user__=user_ok)

    assert result.startswith("CONNECTION CHECK FAILED")
    assert expected in result
    assert PASSWORD not in result


async def test_check_contains_unexpected_errors(tool, user_ok, fake_exchange):
    FakeAccount.raise_on_init = RuntimeError(f"leaky {PASSWORD}")

    result = await tool.check_exchange_connection(__user__=user_ok)

    assert result.startswith("CONNECTION CHECK FAILED")
    assert PASSWORD not in result


async def test_check_status_sequence(tool, user_ok, fake_exchange, recording_emitter):
    tool.valves.emit_status = True

    await tool.check_exchange_connection(__user__=user_ok, __event_emitter__=recording_emitter)

    assert sum(1 for e in recording_emitter.events if e["data"]["done"]) == 1
    assert recording_emitter.events[-1]["data"]["description"] == "Connection OK."


async def test_check_reports_missing_library(monkeypatch, tool, user_ok, fake_exchange):
    monkeypatch.setattr(m, "EXCHANGELIB_AVAILABLE", False)
    monkeypatch.setattr(m, "EXCHANGELIB_IMPORT_ERROR", "ImportError: no module named exchangelib")

    result = await tool.check_exchange_connection(__user__=user_ok)

    assert result.startswith("CONNECTION CHECK FAILED")
    assert "exchangelib" in result
    assert FakeAccount.instances == []
