"""
title: Exchange E-Mail (EWS)
author: Matze2010
author_url: https://github.com/Matze2010/OWAMCPServer
funding_url: https://github.com/Matze2010/OWAMCPServer
version: 1.0.0
license: MIT
requirements: exchangelib>=5.4
description: Send email via an on-premises Exchange server (EWS), using each user's own mailbox credentials.
"""

from __future__ import annotations

import asyncio
import html as html_module
import logging
import re
import threading
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Guarded imports
#
# Open WebUI installs the `requirements:` frontmatter at tool load time, but that
# can be disabled (ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS) or fail offline.
# The module must stay importable either way, so every exchangelib symbol is
# bound to None on failure and the send paths check EXCHANGELIB_AVAILABLE.
# `Exception` rather than `ImportError`: a broken partial install raises other
# things at import time.
# ---------------------------------------------------------------------------
try:
    from exchangelib import (
        DELEGATE,
        Account,
        Build,
        Configuration,
        Credentials,
        HTMLBody,
        Message,
        Version,
    )
    from exchangelib import errors as ews_errors

    try:  # top-level since exchangelib 4.x
        from exchangelib import BaseProtocol, NoVerifyHTTPAdapter
    except ImportError:  # pragma: no cover - older layouts
        from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter

    EXCHANGELIB_AVAILABLE = True
    EXCHANGELIB_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - exercised via reload in tests
    Account = Build = Configuration = Credentials = HTMLBody = Message = Version = None
    BaseProtocol = NoVerifyHTTPAdapter = DELEGATE = None
    ews_errors = None
    EXCHANGELIB_AVAILABLE = False
    EXCHANGELIB_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

try:
    import requests.adapters
    import requests.exceptions

    REQUESTS_AVAILABLE = True
except Exception:  # pragma: no cover - requests only arrives with exchangelib
    requests = None
    REQUESTS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
EMAIL_RE = re.compile(rf"^[^@\s,;<>]+@{_LABEL}(\.{_LABEL})+$")
ANGLE_ADDR_RE = re.compile(r"<([^<>]+)>")
# No `\s*` after `<`: real HTML never separates the bracket from the tag name,
# and allowing it would misread plain prose like "a < b" as markup.
HTML_HINT_RE = re.compile(r"</?(html|body|div|p|br|table|tr|td|ul|ol|li|h[1-6]|span|strong|em|b|i|a\s+href)\b", re.I)
SCRIPT_RE = re.compile(r"<\s*(script|iframe|object|embed)\b.*?<\s*/\s*\1\s*>", re.I | re.S)
DANGLING_TAG_RE = re.compile(r"<\s*/?\s*(script|iframe|object|embed)\b[^>]*>", re.I)
EVENT_ATTR_RE = re.compile(r"""\son\w+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
JS_URL_RE = re.compile(r"""(href|src)\s*=\s*(["'])\s*javascript:[^"']*\2""", re.I)

MAX_EMAIL_LENGTH = 254
BODY_PREVIEW_CHARS = 500
REDACTION_MIN_SECRET_LENGTH = 4

BANNER_SENT = "EMAIL SENT SUCCESSFULLY"
BANNER_DRY_RUN = "DRY RUN - NO EMAIL WAS SENT"
BANNER_NOT_SENT = "EMAIL NOT SENT"

IMPORTANCE_CHOICES = {"low": "Low", "normal": "Normal", "high": "High"}


# ---------------------------------------------------------------------------
# Error mapping
#
# Built by name lookup at import time so that a renamed or removed upstream
# class degrades to the next broader entry instead of raising at import.
# Order matters: ResponseMessageError sits *below* TransportError in the
# exchangelib hierarchy, so the specific classes must be checked first or a
# TransportError clause would swallow every mailbox and permission error.
# ---------------------------------------------------------------------------


def _err(name: str) -> type[BaseException] | None:
    return getattr(ews_errors, name, None) if ews_errors is not None else None


ERROR_MAP: list[tuple[type[BaseException] | None, str]] = [
    (
        _err("ErrorSendAsDenied"),
        "You are not allowed to send as this address. Check that your 'email_address' user valve "
        "matches a mailbox your 'username' may send from.",
    ),
    (
        _err("ErrorNonExistentMailbox"),
        "No mailbox exists for the configured address. Check the 'email_address' user valve.",
    ),
    (
        _err("ErrorAccessDenied"),
        "Exchange denied access. Your account may lack permission for this mailbox or for the Sent Items folder.",
    ),
    (_err("ErrorInvalidSmtpAddress"), "Exchange rejected one of the addresses as invalid."),
    (_err("ErrorInvalidRecipients"), "Exchange rejected one or more recipients."),
    (_err("ErrorMissingEmailAddress"), "A required email address was missing."),
    (_err("ErrorMessageSizeExceeded"), "The message exceeds the size limit configured on the server."),
    (_err("ErrorQuotaExceeded"), "The mailbox quota is exceeded."),
    (_err("ErrorTimeoutExpired"), "Exchange timed out while processing the request. Please try again."),
    (_err("ErrorServerBusy"), "The Exchange server is busy. Please try again shortly."),
    (_err("RateLimitError"), "The Exchange server is rate-limiting requests. Please try again shortly."),
    (
        _err("AutoDiscoverFailed"),
        "Autodiscover could not locate the EWS endpoint. Disable the 'autodiscover' valve and set "
        "'ews_service_endpoint' explicitly.",
    ),
    (
        _err("MalformedResponseError"),
        "The server returned an unexpected response. The configured URL may not point at an EWS endpoint.",
    ),
    (
        _err("UnauthorizedError"),
        "Authentication failed. Check your username and password, and the 'auth_type' valve. "
        "NTLM usually requires the form 'DOMAIN\\username'.",
    ),
    (_err("TransportError"), "Could not communicate with the Exchange server."),
    (_err("EWSError"), "The Exchange server reported an error."),
]

# Error classes that suggest the Sent Items folder was the problem rather than
# the send itself. exchangelib resolves account.sent *before* sending, so when
# one of these fires with save_copy enabled, nothing was delivered.
_SENT_ITEMS_ERROR_NAMES = ("ErrorAccessDenied", "ErrorItemNotFound", "ErrorFolderNotFound")
SENT_ITEMS_HINT_ERRORS = tuple(cls for cls in map(_err, _SENT_ITEMS_ERROR_NAMES) if cls is not None)


# ---------------------------------------------------------------------------
# Process-global protocol settings
#
# exchangelib keeps the timeout and the HTTP adapter as BaseProtocol class
# attributes, so these settings affect every exchangelib user in the same
# Open WebUI process and only take full effect before the first connection is
# opened. Tracked here so a later change can at least be reported honestly.
# ---------------------------------------------------------------------------

_GLOBAL_LOCK = threading.Lock()
_GLOBAL_STATE: dict[str, Any] = {"applied": None}


# ---------------------------------------------------------------------------
# Pure helpers (module level on purpose)
#
# Open WebUI builds the LLM function spec by enumerating callables on the Tools
# instance. Keeping helpers out of the class guarantees they can never surface
# as callable tools, regardless of the Open WebUI version's underscore handling.
# ---------------------------------------------------------------------------


def redact(text: str, secrets: object) -> str:
    """Replace secrets with '***'.

    A secret that is empty or very short is ignored: str.replace("", "***")
    would shred the entire message, which is worse than the leak it prevents.
    """
    if not text:
        return text
    if isinstance(secrets, str):
        secrets = (secrets,)
    for secret in secrets or ():
        if isinstance(secret, str) and len(secret) >= REDACTION_MIN_SECRET_LENGTH:
            text = text.replace(secret, "***")
    return text


def parse_recipients(value: object) -> list[str]:
    """Turn a recipient specification into a deduplicated list of addresses.

    Accepts a comma/semicolon/newline separated string, a list or tuple, or
    None. 'Display Name <addr@example.com>' is reduced to the address.
    Deduplication is case-insensitive but keeps first-seen order and casing.
    """
    if value is None:
        return []

    raw_parts: list[str] = []
    if isinstance(value, (list, tuple, set)):
        for item in value:
            raw_parts.extend(re.split(r"[,;\n]", str(item)))
    else:
        raw_parts = re.split(r"[,;\n]", str(value))

    result: list[str] = []
    seen: set[str] = set()
    for part in raw_parts:
        candidate = part.strip()
        if not candidate:
            continue
        match = ANGLE_ADDR_RE.search(candidate)
        if match:
            candidate = match.group(1).strip()
        candidate = candidate.strip("<>").strip()
        if not candidate:
            continue
        key = candidate.lower()
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def is_valid_email(address: str) -> bool:
    """Pragmatic address check.

    The goal is catching model hallucinations like 'john.doe' or 'john@company',
    not implementing RFC 5322.
    """
    if not address or len(address) > MAX_EMAIL_LENGTH:
        return False
    return bool(EMAIL_RE.match(address))


def split_domain_list(raw: str) -> set[str]:
    return {part.strip().lower() for part in re.split(r"[,;\n]", raw or "") if part.strip()}


def check_domain_policy(addresses: list[str], allowed_raw: str, blocked_raw: str) -> tuple[list[str], str]:
    """Apply the recipient domain policy.

    Returns (rejected_addresses, reason). Matching is exact and case-insensitive
    with no subdomain expansion: an allow-list entry 'example.com' permits
    neither 'mail.example.com' nor 'evil-example.com'. Subdomains must be listed
    explicitly. The block-list is evaluated first and always wins.
    """
    allowed = split_domain_list(allowed_raw)
    blocked = split_domain_list(blocked_raw)

    def domain_of(address: str) -> str:
        return address.rsplit("@", 1)[-1].lower() if "@" in address else ""

    if blocked:
        rejected = [a for a in addresses if domain_of(a) in blocked]
        if rejected:
            return rejected, "the domain is on the block list"

    if allowed:
        rejected = [a for a in addresses if domain_of(a) not in allowed]
        if rejected:
            return rejected, "the domain is not on the allow list"

    return [], ""


def looks_like_html(body: str) -> bool:
    """Conservative HTML detection, so 'a < b' in plain text is not misread."""
    return bool(HTML_HINT_RE.search(body or ""))


def sanitize_html(body: str) -> str:
    """Strip active content from model-generated HTML.

    Cheap insurance against prompt-injected markup reaching a recipient's mail
    client. Applied unconditionally rather than behind a valve.
    """
    cleaned = SCRIPT_RE.sub("", body or "")
    cleaned = DANGLING_TAG_RE.sub("", cleaned)
    cleaned = EVENT_ATTR_RE.sub("", cleaned)
    cleaned = JS_URL_RE.sub(r"\1=\2\2", cleaned)
    return cleaned


def normalise_importance(value: object) -> tuple[str, str]:
    """Map free-form importance to an exchangelib choice.

    Returns (importance, note). exchangelib's ChoiceField accepts exactly
    'Low', 'Normal' and 'High', case-sensitive; models routinely emit 'high'.
    Unknown values fall back to 'Normal' with a visible note rather than
    silently.
    """
    text = str(value or "").strip()
    if not text:
        return "Normal", ""
    resolved = IMPORTANCE_CHOICES.get(text.lower())
    if resolved:
        return resolved, ""
    return "Normal", f"Unknown importance '{text}' was replaced with 'Normal'."


def build_version(build_string: str) -> Any:
    """Turn '15.1.2507.16' into a Version, or None when unset/unparsable."""
    text = (build_string or "").strip()
    if not text or not EXCHANGELIB_AVAILABLE:
        return None
    parts = text.split(".")
    try:
        numbers = [int(p) for p in parts[:4]]
    except ValueError:
        logger.warning("Ignoring unparsable exchange_build value")
        return None
    if len(numbers) < 2:
        return None
    return Version(build=Build(*numbers))


def resolve_endpoint(valves: Any) -> tuple[dict[str, str], str]:
    """Return the Configuration kwargs and a label for display.

    exchangelib raises AttributeError when both `server` and `service_endpoint`
    are passed, so exactly one is produced here. The explicit endpoint wins.
    """
    endpoint = (valves.ews_service_endpoint or "").strip()
    if endpoint:
        return {"service_endpoint": endpoint}, endpoint
    server = (valves.ews_server or "").strip()
    if server:
        return {"server": server}, f"https://{server}/EWS/Exchange.asmx"
    return {}, "(autodiscover)"


def resolve_user_valves(user: object, user_valves_cls: type[BaseModel]) -> BaseModel:
    """Extract UserValves from the injected __user__ dict.

    Tolerates a missing __user__, a missing 'valves' key, an already-built model
    and a plain dict, because different Open WebUI paths supply different shapes.
    """
    if not isinstance(user, dict):
        return user_valves_cls()
    valves = user.get("valves")
    if isinstance(valves, user_valves_cls):
        return valves
    if isinstance(valves, BaseModel):
        try:
            return user_valves_cls(**valves.model_dump())
        except Exception:
            return user_valves_cls()
    if isinstance(valves, dict):
        try:
            return user_valves_cls(**valves)
        except Exception:
            return user_valves_cls()
    return user_valves_cls()


def append_signature(body: str, signature: str, as_html: bool) -> str:
    """Append the user's plain-text signature to a plain or HTML body."""
    text = (signature or "").strip()
    if not text:
        return body
    if as_html:
        escaped = html_module.escape(text).replace("\n", "<br>")
        return f"{body}<br><br>-- <br>{escaped}"
    return f"{body}\n\n-- \n{text}"


def describe_error(exc: BaseException) -> str:
    """Map an exception to a user-facing English explanation."""
    for exc_cls, message in ERROR_MAP:
        if exc_cls is not None and isinstance(exc, exc_cls):
            return message

    if REQUESTS_AVAILABLE:
        if isinstance(exc, requests.exceptions.SSLError):
            return (
                "The Exchange server's TLS certificate could not be verified. Set 'ca_bundle_path' to your "
                "internal CA bundle, or disable 'verify_ssl' as a last resort."
            )
        if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)):
            return "The connection to the Exchange server timed out. Consider raising 'request_timeout'."
        if isinstance(exc, requests.exceptions.ConnectionError):
            return (
                "The Exchange server could not be reached. Check 'ews_server' / 'ews_service_endpoint' "
                "and the network connection."
            )

    if isinstance(exc, TimeoutError):
        return (
            "The operation timed out on the tool side. The message may still have been delivered by the server; "
            "check your Sent Items before retrying."
        )
    if isinstance(exc, AttributeError):
        return (
            "The Exchange connection is misconfigured. Set either 'ews_server' or 'ews_service_endpoint', "
            "not both, and enable 'autodiscover' only when no endpoint is configured."
        )
    return "An unexpected error occurred while talking to Exchange."


# ---------------------------------------------------------------------------
# Blocking exchangelib work (always called via asyncio.to_thread)
# ---------------------------------------------------------------------------


def apply_global_settings(valves: Any) -> list[str]:
    """Apply BaseProtocol-level settings. Process-global side effects."""
    warnings: list[str] = []
    if not EXCHANGELIB_AVAILABLE:
        return warnings

    desired = (bool(valves.verify_ssl), (valves.ca_bundle_path or "").strip(), int(valves.request_timeout))
    with _GLOBAL_LOCK:
        previous = _GLOBAL_STATE["applied"]
        if previous is not None and previous != desired:
            warnings.append(
                "TLS or timeout settings changed after connections were already established. "
                "Restart Open WebUI for them to take full effect."
            )

        BaseProtocol.TIMEOUT = max(5, desired[2])
        verify_ssl, ca_bundle, _ = desired
        if not verify_ssl:
            BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
        elif ca_bundle:
            BaseProtocol.HTTP_ADAPTER_CLS = _make_ca_adapter(ca_bundle)
        elif REQUESTS_AVAILABLE:
            BaseProtocol.HTTP_ADAPTER_CLS = requests.adapters.HTTPAdapter
        _GLOBAL_STATE["applied"] = desired
    return warnings


def _make_ca_adapter(ca_bundle_path: str) -> type:
    class RootCAAdapter(requests.adapters.HTTPAdapter):
        """Force certificate verification against a specific CA bundle."""

        def cert_verify(self, conn, url, verify, cert):  # noqa: ARG002 - signature fixed by requests
            super().cert_verify(conn=conn, url=url, verify=ca_bundle_path, cert=cert)

    return RootCAAdapter


def build_account(valves: Any, user_valves: Any) -> Any:
    """Construct an exchangelib Account. Blocking - call via asyncio.to_thread.

    No caching on purpose: exchangelib's CachingProtocol metaclass already pools
    Protocol instances (and their TLS/NTLM sessions) thread-safely, keyed on
    (service_endpoint, credentials). With autodiscover disabled, constructing an
    Account performs no network calls at all, so a hand-rolled cache would only
    add stale-credential and locking problems.
    """
    credentials = Credentials(username=user_valves.username, password=user_valves.password)
    endpoint_kwargs, _ = resolve_endpoint(valves)
    config = Configuration(
        credentials=credentials,
        auth_type=valves.auth_type,
        version=build_version(valves.exchange_build),
        **endpoint_kwargs,
    )
    return Account(
        primary_smtp_address=user_valves.email_address,
        config=config,
        autodiscover=bool(valves.autodiscover),
        access_type=DELEGATE,
    )


def perform_send(valves: Any, user_valves: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Build and send the message. Blocking - call via asyncio.to_thread."""
    warnings = apply_global_settings(valves)
    account = build_account(valves, user_valves)

    body = HTMLBody(spec["body"]) if spec["is_html"] else spec["body"]
    message = Message(
        account=account,
        subject=spec["subject"],
        body=body,
        to_recipients=list(spec["to"]),
        cc_recipients=list(spec["cc"]),
        bcc_recipients=list(spec["bcc"]),
        importance=spec["importance"],
    )
    if spec["reply_to"]:
        message.reply_to = list(spec["reply_to"])

    # save_copy=True is exchangelib's default and resolves account.sent *before*
    # sending, so a failure on that path means nothing was delivered.
    message.send(save_copy=bool(valves.save_to_sent_items))
    return {"warnings": warnings}


def perform_connection_check(valves: Any, user_valves: Any) -> dict[str, Any]:
    """Open a connection and touch the mailbox. Blocking - via asyncio.to_thread."""
    warnings = apply_global_settings(valves)
    account = build_account(valves, user_valves)
    inbox_name = account.inbox.name  # forces an actual EWS round trip
    return {
        "warnings": warnings,
        "endpoint": account.protocol.service_endpoint,
        "version": str(account.version.fullname) if account.version else "unknown",
        "primary_smtp_address": account.primary_smtp_address,
        "inbox_name": inbox_name,
    }


# ---------------------------------------------------------------------------
# Chat events
# ---------------------------------------------------------------------------


async def emit_status(emitter: Callable[[dict], Awaitable[None]] | None, description: str, done: bool = False) -> None:
    """Emit a status event. UI feedback must never break the tool."""
    if emitter is None:
        return
    try:
        await emitter({"type": "status", "data": {"description": description, "done": done}})
    except Exception:
        logger.debug("Status emit failed", exc_info=True)


async def request_confirmation(event_call: Callable[[dict], Awaitable[Any]], title: str, message: str) -> bool:
    """Ask the user to confirm, and fail closed.

    __event_call__ maps to socket.io's call() and returns whatever the frontend
    answered - but on timeout or a disconnected client it returns
    {"error": "..."}, which is a *truthy* dict. Anything that is not an explicit
    confirmation therefore counts as "not confirmed".
    """
    try:
        response = await event_call({"type": "confirmation", "data": {"title": title, "message": message}})
    except Exception:
        logger.debug("Confirmation call failed", exc_info=True)
        return False

    if response is True:
        return True
    if isinstance(response, dict) and response.get("confirmed") is True:
        return True
    return False


# ---------------------------------------------------------------------------
# Output blocks
# ---------------------------------------------------------------------------


def _format_addresses(addresses: list[str]) -> str:
    return ", ".join(addresses) if addresses else "(none)"


def failure_block(reason: str, extra: str = "") -> str:
    block = f"{BANNER_NOT_SENT}\nReason: {reason}"
    if extra:
        block += f"\n{extra}"
    return block


def success_block(spec: dict[str, Any], valves: Any, notes: list[str]) -> str:
    bcc_count = len(spec["bcc"])
    lines = [
        BANNER_SENT,
        f"From:       {spec['sender']}",
        f"To:         {_format_addresses(spec['to'])}",
        f"Cc:         {_format_addresses(spec['cc'])}",
        # Bcc addresses are reported as a count only: echoing them back into the
        # chat transcript would defeat the point of using Bcc.
        f"Bcc:        {bcc_count} recipient(s)" if bcc_count else "Bcc:        (none)",
        f"Subject:    {spec['subject']}",
        f"Format:     {'HTML' if spec['is_html'] else 'plain text'}",
        f"Importance: {spec['importance']}",
        f"Saved to Sent Items: {'yes' if valves.save_to_sent_items else 'no'}",
    ]
    lines.extend(notes)
    return "\n".join(lines)


def dry_run_block(spec: dict[str, Any], valves: Any, endpoint_label: str, notes: list[str]) -> str:
    body = spec["body"]
    preview = body[:BODY_PREVIEW_CHARS]
    truncated = " […]" if len(body) > BODY_PREVIEW_CHARS else ""
    lines = [
        BANNER_DRY_RUN,
        "The tool is in simulation mode (valve 'dry_run' is enabled). No connection to Exchange was",
        "made and nothing was delivered.",
        "",
        "The following message WOULD have been sent:",
        f"From:       {spec['sender']}",
        f"To:         {_format_addresses(spec['to'])}",
        f"Cc:         {_format_addresses(spec['cc'])}",
        f"Bcc:        {_format_addresses(spec['bcc'])}",
        f"Reply-To:   {_format_addresses(spec['reply_to'])}",
        f"Subject:    {spec['subject']}",
        f"Format:     {'HTML' if spec['is_html'] else 'plain text'}",
        f"Importance: {spec['importance']}",
        f"Server:     {endpoint_label} (auth: {valves.auth_type})",
        f"Saved to Sent Items: {'yes' if valves.save_to_sent_items else 'no'}",
        f"Body preview (first {BODY_PREVIEW_CHARS} characters):",
        "---",
        f"{preview}{truncated}",
        "---",
    ]
    lines.extend(notes)
    lines.append("")
    lines.append(
        "Credentials, TLS and reachability are NOT verified by a dry run - use check_exchange_connection for that."
    )
    lines.append(f"{BANNER_DRY_RUN}. Disable the 'dry_run' valve to send for real.")
    return "\n".join(lines)


def missing_library_message() -> str:
    return failure_block(
        "the Python library 'exchangelib' is not available, so no connection to Exchange can be made.",
        "An administrator should verify that 'requirements: exchangelib>=5.4' is present in the tool header "
        f"and that Open WebUI was able to install it. Details: {EXCHANGELIB_IMPORT_ERROR or 'unknown import error'}",
    )


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class Tools:
    class Valves(BaseModel):
        ews_server: str = Field(
            default="",
            description="Exchange server hostname, e.g. 'mail.example.com'. Ignored when "
            "'ews_service_endpoint' is set. Leave empty when using autodiscover.",
        )
        ews_service_endpoint: str = Field(
            default="",
            description="Full EWS URL, e.g. 'https://mail.example.com/EWS/Exchange.asmx'. Takes precedence "
            "over 'ews_server'. Use this when the EWS path is non-standard.",
        )
        auth_type: Literal["NTLM", "BASIC", "GSSAPI", "SSPI", "CBA"] = Field(
            default="NTLM",
            description="EWS authentication method. NTLM is correct for most on-premises Exchange servers.",
        )
        autodiscover: bool = Field(
            default=False,
            description="Use Exchange Autodiscover to locate the EWS endpoint instead of the configured server. "
            "Slower and often blocked on-premises; keep this disabled if you know the endpoint.",
        )
        exchange_build: str = Field(
            default="",
            description="Optional Exchange build to pin, e.g. '15.1.2507.16'. Pinning skips server version "
            "detection and saves a request per connection. Leave empty to auto-detect.",
        )
        verify_ssl: bool = Field(
            default=True,
            description="Verify the Exchange server's TLS certificate. WARNING: this setting affects the entire "
            "Open WebUI process, not just this tool. Prefer 'ca_bundle_path' over disabling it.",
        )
        ca_bundle_path: str = Field(
            default="",
            description="Path to a CA bundle (PEM) used to validate the Exchange certificate, for internal or "
            "self-signed CAs. Applies process-wide. Ignored when 'verify_ssl' is disabled.",
        )
        request_timeout: int = Field(
            default=60,
            description="Timeout in seconds for a single EWS request. Applies process-wide and only takes full "
            "effect before the first connection is opened.",
        )
        require_confirmation: bool = Field(
            default=True,
            description="Ask the user to confirm in the chat before an email is actually sent. Without a "
            "confirmation nothing is sent. Disable only if you accept unattended sending.",
        )
        dry_run: bool = Field(
            default=False,
            description="Simulation mode: validate and render the message but never contact Exchange and never "
            "send. Use this to test the configuration safely.",
        )
        save_to_sent_items: bool = Field(
            default=True,
            description="Save a copy of each sent message in the user's Sent Items folder. Requires access to "
            "that folder; disable it if the mailbox denies access.",
        )
        allowed_recipient_domains: str = Field(
            default="",
            description="Comma-separated allow list of recipient domains, e.g. 'example.com, example.org'. "
            "Empty means all domains are allowed. Matching is exact; subdomains must be listed separately.",
        )
        blocked_recipient_domains: str = Field(
            default="",
            description="Comma-separated deny list of recipient domains. Takes precedence over the allow list. "
            "Matching is exact and case-insensitive.",
        )
        max_recipients: int = Field(
            default=25,
            description="Maximum total number of recipients (To + Cc + Bcc) per message. The message is "
            "rejected entirely when the limit is exceeded.",
        )
        auto_detect_html: bool = Field(
            default=True,
            description="Treat a body as HTML when it clearly contains HTML markup, even if the model did not "
            "set the HTML flag. Prevents recipients from seeing raw tags.",
        )
        emit_status: bool = Field(
            default=True,
            description="Show progress messages (validating, connecting, sending) in the chat.",
        )
        debug_errors: bool = Field(
            default=False,
            description="Append the technical exception type and message to error results. Helpful when "
            "diagnosing authentication or certificate problems.",
        )

    class UserValves(BaseModel):
        enabled: bool = Field(
            default=True,
            description="Allow this tool to send email on your behalf.",
        )
        username: str = Field(
            default="",
            description="Your Exchange login name, usually 'DOMAIN\\username' with a single backslash. "
            "Depending on the server your UPN or email address may work too.",
        )
        email_address: str = Field(
            default="",
            description="Your primary SMTP address, e.g. 'first.last@example.com'. This is the mailbox "
            "messages are sent from.",
        )
        password: str = Field(
            default="",
            description="Your Exchange password. Open WebUI stores valve values in its database, so treat this "
            "as stored in clear text and pick an account accordingly.",
        )
        signature: str = Field(
            default="",
            description="Optional plain-text signature appended to every message you send.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        body_is_html: bool = False,
        importance: str = "Normal",
        reply_to: str = "",
        __user__: dict | None = None,
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
        __event_call__: Callable[[dict], Awaitable[Any]] | None = None,
    ) -> str:
        """
        Send an email from the user's own Exchange mailbox.

        The user is asked to confirm before the message is actually sent.

        :param to: Recipient email addresses separated by commas. At least one is required.
        :param subject: Subject line of the email. Must not be empty.
        :param body: Message body. Plain text by default, or HTML when body_is_html is true.
        :param cc: Optional Cc recipients separated by commas.
        :param bcc: Optional Bcc recipients separated by commas.
        :param body_is_html: Set to true when the body contains HTML markup instead of plain text.
        :param importance: Message importance: Low, Normal or High. Defaults to Normal.
        :param reply_to: Optional reply-to addresses separated by commas.
        """
        valves = self.valves
        user_valves = resolve_user_valves(__user__, self.UserValves)
        secrets = (user_valves.password,)

        async def status(description: str, done: bool = False) -> None:
            if valves.emit_status:
                await emit_status(__event_emitter__, redact(description, secrets), done)

        async def fail(reason: str, extra: str = "") -> str:
            await status("Sending failed.", done=True)
            return redact(failure_block(reason, extra), secrets)

        try:
            await status("Checking configuration…")

            if not user_valves.enabled:
                return await fail("email sending is disabled in your personal tool settings.")

            missing = [
                name
                for name, value in (
                    ("username", user_valves.username),
                    ("email_address", user_valves.email_address),
                    ("password", user_valves.password),
                )
                if not (value or "").strip()
            ]
            if missing:
                return await fail(
                    f"your Exchange credentials are incomplete. Missing user valve(s): {', '.join(missing)}.",
                    "Open the tool's user settings (Valves) and fill them in.",
                )

            sender = user_valves.email_address.strip()
            if not is_valid_email(sender):
                return await fail(f"the configured sender address '{sender}' is not a valid email address.")

            await status("Validating recipients…")

            to_list = parse_recipients(to)
            cc_list = parse_recipients(cc)
            bcc_list = parse_recipients(bcc)
            reply_to_list = parse_recipients(reply_to)

            if not to_list:
                return await fail("no recipient was provided in 'to'.")

            everyone = to_list + cc_list + bcc_list + reply_to_list
            invalid = [a for a in everyone if not is_valid_email(a)]
            if invalid:
                return await fail(f"these addresses are not valid: {', '.join(invalid)}.")

            total = len(to_list) + len(cc_list) + len(bcc_list)
            if total > valves.max_recipients:
                return await fail(
                    f"the message has {total} recipients, which exceeds the configured limit of "
                    f"{valves.max_recipients}."
                )

            rejected, policy_reason = check_domain_policy(
                to_list + cc_list + bcc_list,
                valves.allowed_recipient_domains,
                valves.blocked_recipient_domains,
            )
            if rejected:
                return await fail(
                    f"{len(rejected)} recipient(s) are not permitted because {policy_reason}: {', '.join(rejected)}."
                )

            clean_subject = (subject or "").strip()
            if not clean_subject:
                return await fail("the subject must not be empty.")

            clean_body = (body or "").strip()
            if not clean_body:
                return await fail("the message body must not be empty.")

            notes: list[str] = []
            resolved_importance, importance_note = normalise_importance(importance)
            if importance_note:
                notes.append(f"Note: {importance_note}")

            is_html = bool(body_is_html)
            if not is_html and valves.auto_detect_html and looks_like_html(clean_body):
                is_html = True
                notes.append("Note: the body was detected as HTML and sent as HTML.")
            if is_html:
                clean_body = sanitize_html(clean_body)
            clean_body = append_signature(clean_body, user_valves.signature, is_html)

            spec = {
                "sender": sender,
                "to": to_list,
                "cc": cc_list,
                "bcc": bcc_list,
                "reply_to": reply_to_list,
                "subject": clean_subject,
                "body": clean_body,
                "is_html": is_html,
                "importance": resolved_importance,
            }

            _, endpoint_label = resolve_endpoint(valves)

            if valves.dry_run:
                await status("Dry run - no email was sent.", done=True)
                return redact(dry_run_block(spec, valves, endpoint_label, notes), secrets)

            if not EXCHANGELIB_AVAILABLE:
                await status("Sending failed.", done=True)
                return redact(missing_library_message(), secrets)

            if valves.require_confirmation:
                if __event_call__ is None:
                    return await fail(
                        "a confirmation is required before sending, but this chat session cannot show a "
                        "confirmation dialog.",
                        "An administrator can disable the 'require_confirmation' valve to allow unattended sending.",
                    )
                await status("Waiting for your confirmation…")
                bcc_note = f"\nBcc: {len(bcc_list)} recipient(s)" if bcc_list else ""
                confirmed = await request_confirmation(
                    __event_call__,
                    "Send this email?",
                    f"From: {sender}\n"
                    f"To: {_format_addresses(to_list)}\n"
                    f"Cc: {_format_addresses(cc_list)}"
                    f"{bcc_note}\n"
                    f"Subject: {clean_subject}\n"
                    f"Format: {'HTML' if is_html else 'plain text'}\n\n"
                    "Send it now?",
                )
                if not confirmed:
                    await status("Cancelled - no email was sent.", done=True)
                    return redact(
                        failure_block(
                            "the send was not confirmed.",
                            "Nothing was sent. Ask again to retry.",
                        ),
                        secrets,
                    )

            await status("Connecting to Exchange…")
            try:
                # wait_for cancels the await, not the worker thread - the thread
                # keeps running until exchangelib's own BaseProtocol.TIMEOUT
                # fires. The inner timeout is the real one; this is a backstop
                # so a wedged thread cannot block the chat turn forever.
                result = await asyncio.wait_for(
                    asyncio.to_thread(perform_send, valves, user_valves, spec),
                    timeout=max(15, valves.request_timeout) + 15,
                )
            except Exception as exc:
                logger.warning("Sending failed: %s", type(exc).__name__)
                reason = describe_error(exc)
                extra_lines = []
                if valves.save_to_sent_items and SENT_ITEMS_HINT_ERRORS and isinstance(exc, SENT_ITEMS_HINT_ERRORS):
                    extra_lines.append(
                        "This can also mean the Sent Items folder is inaccessible. Nothing was sent; try again "
                        "with the 'save_to_sent_items' valve disabled."
                    )
                if valves.debug_errors:
                    extra_lines.append(f"Technical detail: {type(exc).__name__}: {exc}")
                return await fail(reason, "\n".join(extra_lines))

            notes.extend(f"Warning: {w}" for w in result.get("warnings", []))
            await status(f"Email sent to {total} recipient(s).", done=True)
            return redact(success_block(spec, valves, notes), secrets)

        except Exception as exc:  # last-resort guard: a tool must never raise
            logger.exception("Unexpected error in send_email")
            await status("Sending failed.", done=True)
            return redact(
                failure_block(
                    "an unexpected internal error occurred.",
                    f"Technical detail: {type(exc).__name__}: {exc}" if self.valves.debug_errors else "",
                ),
                secrets,
            )

    async def check_exchange_connection(
        self,
        __user__: dict | None = None,
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
    ) -> str:
        """
        Verify that the configured Exchange server can be reached and that the user's
        credentials work. Does not send any email.
        """
        valves = self.valves
        user_valves = resolve_user_valves(__user__, self.UserValves)
        secrets = (user_valves.password,)

        async def status(description: str, done: bool = False) -> None:
            if valves.emit_status:
                await emit_status(__event_emitter__, redact(description, secrets), done)

        try:
            if not user_valves.enabled:
                await status("Connection check skipped.", done=True)
                return redact("CONNECTION CHECK FAILED\nThis tool is disabled in your personal settings.", secrets)

            missing = [
                name
                for name, value in (
                    ("username", user_valves.username),
                    ("email_address", user_valves.email_address),
                    ("password", user_valves.password),
                )
                if not (value or "").strip()
            ]
            if missing:
                await status("Connection check failed.", done=True)
                return redact(
                    "CONNECTION CHECK FAILED\n"
                    f"Your Exchange credentials are incomplete. Missing user valve(s): {', '.join(missing)}.",
                    secrets,
                )

            if not EXCHANGELIB_AVAILABLE:
                await status("Connection check failed.", done=True)
                return redact(
                    "CONNECTION CHECK FAILED\n"
                    "The Python library 'exchangelib' is not available. An administrator should verify that "
                    "'requirements: exchangelib>=5.4' is present in the tool header and that Open WebUI was "
                    f"able to install it. Details: {EXCHANGELIB_IMPORT_ERROR or 'unknown import error'}",
                    secrets,
                )

            await status("Connecting to Exchange…")
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(perform_connection_check, valves, user_valves),
                    timeout=max(15, valves.request_timeout) + 15,
                )
            except Exception as exc:
                logger.warning("Connection check failed: %s", type(exc).__name__)
                await status("Connection failed.", done=True)
                detail = f"\nTechnical detail: {type(exc).__name__}: {exc}" if valves.debug_errors else ""
                return redact(f"CONNECTION CHECK FAILED\n{describe_error(exc)}{detail}", secrets)

            await status("Connection OK.", done=True)
            lines = [
                "CONNECTION CHECK OK",
                f"Endpoint:   {result['endpoint']}",
                f"Auth type:  {valves.auth_type}",
                f"Mailbox:    {result['primary_smtp_address']}",
                f"Server:     {result['version']}",
                f"Inbox:      {result['inbox_name']}",
            ]
            lines.extend(f"Warning: {w}" for w in result.get("warnings", []))
            if valves.dry_run:
                lines.append(
                    "Note: the 'dry_run' valve is enabled, so send_email will simulate only. "
                    "This connection check always connects for real."
                )
            return redact("\n".join(lines), secrets)

        except Exception as exc:  # last-resort guard: a tool must never raise
            logger.exception("Unexpected error in check_exchange_connection")
            await status("Connection failed.", done=True)
            detail = f"\nTechnical detail: {type(exc).__name__}: {exc}" if self.valves.debug_errors else ""
            return redact(f"CONNECTION CHECK FAILED\nAn unexpected internal error occurred.{detail}", secrets)
