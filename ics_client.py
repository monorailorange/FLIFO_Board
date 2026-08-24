"""Fetches the raw .ics payload from the subscribed calendar."""
from __future__ import annotations

import logging

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)


class IcsFetchError(RuntimeError):
    pass


# Header names (substring match, case-insensitive) whose values get redacted
# in debug output -- credentials, tokens, session cookies, etc.
_SENSITIVE_HEADER_PATTERNS = (
    "authorization", "cookie", "api-key", "apikey", "token", "secret", "password",
)


def _mask_value(value: str) -> str:
    value = str(value)
    if len(value) <= 8:
        return "*** (masked)"
    return f"{value[:4]}...{value[-4:]} (masked, {len(value)} chars)"


def _mask_headers(headers) -> dict:
    return {
        k: (_mask_value(v) if any(p in k.lower() for p in _SENSITIVE_HEADER_PATTERNS) else v)
        for k, v in headers.items()
    }


def _log_exception_chain(exc: BaseException) -> None:
    logger.info("[ICS_DEBUG]   %s: %s", type(exc).__name__, exc)
    cause = exc.__cause__ or exc.__context__
    depth = 0
    while cause is not None and depth < 6:
        logger.info("[ICS_DEBUG]   caused by: %s: %s", type(cause).__name__, cause)
        cause = cause.__cause__ or cause.__context__
        depth += 1


def fetch_ics(
    url: str,
    username: str = "",
    password: str = "",
    extra_headers: dict | None = None,
    timeout: int = 30,
    debug: bool = False,
) -> bytes:
    """
    GET the subscribed calendar and return the raw .ics bytes.

    Auth: HTTP Basic (username/password), which is the common scheme for
    subscribed-calendar links. `extra_headers` is merged in as-is so
    whatever bespoke headers the host eventually requires can be added
    via ICS_EXTRA_HEADERS in .env without touching this code.

    When `debug` is true (ICS_DEBUG=true in .env), logs the exact outgoing
    request and whatever response info is available -- including the full
    exception chain if the server closes the connection before responding
    at all. Sensitive header values are redacted before logging.
    """
    if not url:
        raise IcsFetchError("ICS_URL is not configured (see .env)")

    headers = dict(extra_headers or {})
    auth = HTTPBasicAuth(username, password) if username or password else None

    session = requests.Session()
    prepared = session.prepare_request(requests.Request("GET", url, headers=headers, auth=auth))

    if debug:
        logger.info("[ICS_DEBUG] ===== Outgoing request =====")
        logger.info("[ICS_DEBUG] %s %s", prepared.method, prepared.url)
        for name, value in _mask_headers(prepared.headers).items():
            logger.info("[ICS_DEBUG]   %s: %s", name, value)
        logger.info("[ICS_DEBUG] auth: %s", "HTTP Basic (credentials masked)" if auth else "none")

    try:
        response = session.send(prepared, timeout=timeout)
    except requests.RequestException as exc:
        if debug:
            logger.info("[ICS_DEBUG] ===== Request failed before any response was received =====")
            _log_exception_chain(exc)
        raise IcsFetchError(f"Failed to fetch calendar: {exc}") from exc

    if debug:
        logger.info("[ICS_DEBUG] ===== Response =====")
        logger.info("[ICS_DEBUG] status: %s %s", response.status_code, response.reason)
        for name, value in response.headers.items():
            logger.info("[ICS_DEBUG]   %s: %s", name, value)
        preview = response.content[:500]
        logger.info("[ICS_DEBUG] body preview (first 500 bytes): %r", preview)

    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise IcsFetchError(f"Failed to fetch calendar: {exc}") from exc

    if not response.content:
        raise IcsFetchError("Calendar host returned an empty response")

    return response.content
