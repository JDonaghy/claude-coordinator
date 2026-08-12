"""Delivery seam for the fleet notifier (#1632).

Delivery is an HTTP POST to a **self-hosted ntfy on the daemon host**,
reachable over Tailscale.  Nothing leaves the tailnet, which matters
because event text carries repo names, issue titles and failure detail.
The operator is on Android, so the ntfy client holds an instant-delivery
connection straight to that server — no relay, no third party.

The notifier's coupling to ntfy is confined to :class:`NtfyTransport`.
Pushover, web-push and e-mail can be added later by implementing
:class:`Transport` without touching the predicate.

**Isolation is the load-bearing property here.**  #1485 is the precedent:
`/health` data was read as authoritative and silently degraded review
routing.  An unreachable ntfy server must not affect dispatch, routing,
the board, or any verdict — so :meth:`Transport.send` returns a bool and
*never* raises, every exception is swallowed at the boundary below, and
callers treat a failed send as "not delivered yet", never as an error to
propagate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from coord.notifier.models import Message

log = logging.getLogger(__name__)

#: HTTP header values must be latin-1 encodable.  Issue titles are not.
_HEADER_SAFE_FALLBACK = "?"


def _header_safe(value: str) -> str:
    """Coerce *value* into something an HTTP header can carry.

    ntfy reads the title/tags/click out of headers, and an issue title with
    an em dash or a CJK character would otherwise raise
    ``UnicodeEncodeError`` deep inside the HTTP client — turning a cosmetic
    problem into a failed delivery.
    """
    cleaned = value.replace("\n", " ").replace("\r", " ").strip()
    return cleaned.encode("latin-1", "replace").decode("latin-1") or _HEADER_SAFE_FALLBACK


@dataclass(frozen=True)
class SendResult:
    ok: bool
    error: str | None = None
    status: int | None = None


class Transport(Protocol):
    """Anything that can put a :class:`Message` in front of the operator."""

    name: str

    def send(self, message: Message) -> SendResult:
        """Deliver *message*.  MUST NOT raise, whatever happens."""
        ...  # pragma: no cover - protocol


@dataclass
class NullTransport:
    """Drops everything, successfully.

    Used by ``--dry-run`` and by a deployment that has not configured a
    transport yet.  It reports success so a dry run exercises the same
    ledger/dedupe path a real one would.
    """

    name: str = "null"
    sent: list[Message] = field(default_factory=list)

    def send(self, message: Message) -> SendResult:
        self.sent.append(message)
        log.debug("notifier(null): %s", message.title)
        return SendResult(ok=True)


@dataclass
class MemoryTransport:
    """In-process transport for tests.

    ``fail`` makes every send report failure without raising — the exact
    shape of an unreachable ntfy server, which the isolation test drives.
    """

    name: str = "memory"
    sent: list[Message] = field(default_factory=list)
    fail: bool = False

    def send(self, message: Message) -> SendResult:
        if self.fail:
            return SendResult(ok=False, error="memory transport configured to fail")
        self.sent.append(message)
        return SendResult(ok=True)


@dataclass
class NtfyTransport:
    """POST to a self-hosted ntfy server.

    ``base_url`` is the server root (``http://dellserver:7440``), ``topic``
    the topic the phone is subscribed to.  ``token`` is optional and only
    meaningful on a server with auth enabled — on a tailnet-only server
    there is often nothing to authenticate against.
    """

    base_url: str
    topic: str
    token: str | None = None
    timeout: float = 5.0
    name: str = "ntfy"

    @property
    def url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.topic.lstrip('/')}"

    def send(self, message: Message) -> SendResult:
        # Local import: the base install is a thin client (#1237) and this
        # module is imported by `coord notifier --help`, which must not pay
        # for an HTTP stack it is not going to use.
        import httpx  # noqa: PLC0415

        headers = {
            "Title": _header_safe(message.title),
            "Priority": str(int(message.priority)),
        }
        if message.tags:
            headers["Tags"] = _header_safe(",".join(message.tags))
        if message.click_url:
            headers["Click"] = _header_safe(message.click_url)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            response = httpx.post(
                self.url,
                content=message.body.encode("utf-8"),
                headers=headers,
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 — advisory channel, see module docstring
            log.debug("notifier(ntfy): send failed: %s", exc)
            return SendResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        if response.status_code >= 400:
            return SendResult(
                ok=False,
                status=response.status_code,
                error=f"ntfy returned HTTP {response.status_code}",
            )
        return SendResult(ok=True, status=response.status_code)


def safe_send(transport: Transport, message: Message) -> SendResult:
    """Send through *transport*, converting any escape into a failed result.

    The belt to :meth:`NtfyTransport.send`'s braces.  A third-party
    transport added later cannot break the daemon tick by raising, because
    nothing above this function ever sees an exception from delivery.
    """
    try:
        return transport.send(message)
    except Exception as exc:  # noqa: BLE001 — see module docstring
        log.warning("notifier: transport %r raised: %s", getattr(transport, "name", "?"), exc)
        return SendResult(ok=False, error=f"{type(exc).__name__}: {exc}")


def build_transport(cfg: "object") -> Transport:
    """Build the configured transport from a ``NotificationsConfig``.

    Falls back to :class:`NullTransport` — never to an exception — when the
    configuration is incomplete.  A half-configured notifier is a notifier
    that says nothing, not a coordinator that fails to start.
    """
    kind = str(getattr(cfg, "transport", "ntfy") or "ntfy")
    if kind == "none":
        return NullTransport()
    if kind == "ntfy":
        base_url = getattr(cfg, "ntfy_url", None)
        topic = getattr(cfg, "ntfy_topic", None)
        if not base_url or not topic:
            log.debug("notifier: ntfy transport not configured (url/topic missing)")
            return NullTransport()
        return NtfyTransport(
            base_url=str(base_url),
            topic=str(topic),
            token=getattr(cfg, "ntfy_token", None),
            timeout=float(getattr(cfg, "timeout_secs", 5.0) or 5.0),
        )
    log.warning("notifier: unknown transport %r — nothing will be delivered", kind)
    return NullTransport()
