"""Network health checks for agent servers over Tailscale (plain HTTP).

Tailscale's MagicDNS resolves hostnames and the tailnet encrypts the
connection, so we use plain HTTP on the agent port. This module classifies
connection failures so the CLI can give actionable diagnostics rather than
a generic "offline".
"""

from __future__ import annotations

import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable

import httpx

from coord.models import Machine

AGENT_PORT = 7433
DEFAULT_TIMEOUT = 3.0

# Status categories — keep these stable; downstream code may key off them.
ONLINE = "online"
OFFLINE = "offline"
TIMEOUT = "timeout"
DNS_ERROR = "dns_error"
HTTP_ERROR = "http_error"
UNKNOWN = "unknown"


@dataclass
class MachineStatus:
    machine: Machine
    state: str
    reason: str = ""
    latency_ms: float | None = None
    health: dict | None = None

    @property
    def is_online(self) -> bool:
        return self.state == ONLINE


def classify_error(exc: Exception) -> tuple[str, str]:
    """Map an httpx/network exception to a (state, reason) pair."""
    if isinstance(exc, httpx.ConnectTimeout) or isinstance(exc, httpx.ReadTimeout):
        return TIMEOUT, "timed out"
    if isinstance(exc, httpx.ConnectError):
        msg = str(exc).lower()
        if "name or service not known" in msg or "nodename nor servname" in msg or "getaddrinfo" in msg:
            return DNS_ERROR, "hostname not resolvable (Tailscale up?)"
        if "connection refused" in msg:
            return OFFLINE, "connection refused (agent not running?)"
        return OFFLINE, f"connection failed ({exc})"
    if isinstance(exc, socket.gaierror):
        return DNS_ERROR, "hostname not resolvable (Tailscale up?)"
    if isinstance(exc, httpx.HTTPError):
        return HTTP_ERROR, f"http error: {exc}"
    return UNKNOWN, f"{type(exc).__name__}: {exc}"


def check_machine(machine: Machine, timeout: float = DEFAULT_TIMEOUT) -> MachineStatus:
    """Ping `machine`'s /health endpoint and classify the result."""
    url = f"http://{machine.host}:{AGENT_PORT}/health"
    start = time.perf_counter()
    try:
        resp = httpx.get(url, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — we classify all network errors
        state, reason = classify_error(e)
        return MachineStatus(machine=machine, state=state, reason=reason)

    latency_ms = (time.perf_counter() - start) * 1000.0
    if resp.status_code != 200:
        return MachineStatus(
            machine=machine,
            state=HTTP_ERROR,
            reason=f"HTTP {resp.status_code}",
            latency_ms=latency_ms,
        )
    try:
        health = resp.json()
    except ValueError:
        return MachineStatus(
            machine=machine,
            state=HTTP_ERROR,
            reason="invalid JSON from /health",
            latency_ms=latency_ms,
        )
    return MachineStatus(
        machine=machine,
        state=ONLINE,
        reason="",
        latency_ms=latency_ms,
        health=health,
    )


def check_all(
    machines: Iterable[Machine],
    timeout: float = DEFAULT_TIMEOUT,
    max_workers: int | None = None,
) -> list[MachineStatus]:
    """Health-check every machine concurrently. Preserves input order."""
    machines = list(machines)
    if not machines:
        return []
    workers = max_workers or min(8, len(machines))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda m: check_machine(m, timeout=timeout), machines))


def fetch_status(machine: Machine, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    """GET /status from a machine. Returns None on error (caller already checked health)."""
    try:
        resp = httpx.get(
            f"http://{machine.host}:{AGENT_PORT}/status", timeout=timeout
        )
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError):
        return None


def fetch_repos(machine: Machine, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    """GET /repos. Returns None on network error (per-repo errors come back inside the dict)."""
    try:
        resp = httpx.get(
            f"http://{machine.host}:{AGENT_PORT}/repos", timeout=timeout
        )
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError):
        return None


def fetch_log(
    machine: Machine,
    assignment_id: str,
    *,
    since: int = 0,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int, bytes]:
    """GET /logs/{id} from a machine. Returns (status_code, body)."""
    url = f"http://{machine.host}:{AGENT_PORT}/logs/{assignment_id}"
    params = {"since": since} if since else None
    resp = httpx.get(url, params=params, timeout=timeout)
    return resp.status_code, resp.content
