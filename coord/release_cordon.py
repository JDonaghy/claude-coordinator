"""Release cordons: **create** a propagation window instead of waiting for one
(#2101).

`coord release propagate` (#1835/#2067) waits for fleet quiescence. On a fleet
whose drive queue refills every three minutes, that window is a coincidence —
and two facts make the coincidence rare enough to never arrive:

1. the daemon host may not be rolled while it is busy (a caller on a newer
   ``coord`` than its daemon is the documented 405), and nothing may roll
   ahead of the daemon, so **a busy daemon host defers the whole fleet**;
2. every drive-queue entry charges *some* host as busy for the entire life of
   the drive, and the queue relaunches on a 3-minute tick.

Observed on 2026-08-10: the fleet sat eleven releases behind for a day with
elitebook idle and rollable the whole time.

This module is the decision half of the fix. Waiting is replaced by a loop
that manufactures the window:

    detect → **cordon** each behind host → drain (nothing is killed) →
    roll the moment it empties → **uncordon immediately** → repeat.

THE CORDON IS A ROUTING PAUSE WITH AN OWNER
--------------------------------------------
`coord pause` already means exactly "no NEW agents route here; in-flight work
is untouched" (#1563 made it daemon-backed, which is the only reason this is
buildable at all). A cordon reuses that routing semantics — see
:mod:`coord.machine_pause`, which folds active cordons into the one
``paused_set()`` every dispatcher already consults — but it is emphatically
**not** the same flag:

* an operator's ``coord unpause`` must not lift a cordon mid-drain;
* the post-roll uncordon must not clear a pause an operator set deliberately.

So a cordon is stored under its own key with an ``owner``, and each side
clears only its own (:func:`coord.machine_pause.clear_cordon` vs
``local_unpause``). Trap A of #2101.

EVERY CORDON EXPIRES, BECAUSE THE THING THAT SET IT GETS RESTARTED
-------------------------------------------------------------------
The cordon lives in daemon state and the daemon itself is restarted by the
roll it is gating. A propagate run killed between "cordon" and "uncordon"
would otherwise leave every machine refusing work forever — which looks
exactly like a quiet fleet, i.e. #2082 in a new costume. Trap B.

So a :class:`Cordon` carries ``owner``, ``reason``, ``created_at`` and
``expires_at``, and **the read side ignores an expired record** (nothing has
to run for it to lapse — a dead propagate loop cannot fail to clean up,
because cleanup is not an action). The live loop renews on every run while
the host is still behind, so a TTL comfortably longer than the propagate
timer's interval is invisible in normal operation and self-healing after a
crash.

A HOST THAT NEVER DRAINS IS AN ESCALATION, NEVER A SILENT WAIT
---------------------------------------------------------------
A wedged worker means the cordon never lifts. :func:`plan_cordons` measures
the drain from ``created_at`` (preserved across renewals — a renewal is not
a new cordon) and, past :data:`DEFAULT_DRAIN_DEADLINE_SECONDS`, emits a
:class:`DrainEscalation`. The cordon is still renewed — the host really is
behind, and lifting it would just start work that the next run has to drain
again — but it is now loud, and its message names the override. Trap C.

THE TRIGGER IS COUPLED TO RELEASE FREQUENCY, SO IT IS A KNOB
--------------------------------------------------------------
Cordon-on-any-drift costs one fleet drain per release. Before #2081 landed,
releases cut roughly every 40 minutes — at that cadence this mechanism would
leave the fleet draining more often than working. #2081 reduced the cadence,
so the **default is any drift** (:data:`DEFAULT_DRIFT_THRESHOLD` = 1): a
fleet that is one release behind is a fleet running code nobody is testing,
and #2082 is the cost of tolerating that. The threshold is a knob
(``coord release propagate --cordon-after N``) precisely so a future cadence
change does not need a code change. Trap F.

PURITY
------
Nothing here reads the clock, the filesystem, the network or the DB — same
split :mod:`coord.release_propagate` documents, for the same reason. The
clock is passed in; ``coord/commands/release.py`` is the I/O shell.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

#: Who set a cordon. An operator's ``coord pause`` is NOT an owner of any
#: cordon — it lives under a different key entirely (see the module docstring
#: and :mod:`coord.machine_pause`). This exists so a future second automated
#: owner (a deploy gate, a maintenance window) can share the mechanism
#: without either being able to clear the other's flag.
OWNER_RELEASE = "release"

#: How long a cordon record stays effective without being renewed. The
#: propagate timer runs every 15-20 minutes and renews on every run while the
#: host is still behind, so this is invisible in normal operation; what it
#: bounds is the crash case (trap B): a run killed between cordon and roll
#: leaves the fleet refusing work for at most this long, with no cleanup
#: process required — an expired record is ignored on READ.
DEFAULT_TTL_SECONDS = 3600.0

#: How long a host may fail to drain before the cordon escalates (trap C).
#: Measured from the cordon's ``created_at``, which survives renewal — a
#: renewal is the same cordon, not a new one, so a wedged host cannot reset
#: its own deadline by being cordoned again. Deliberately longer than a
#: normal drive (the thing being drained) and shorter than a night.
DEFAULT_DRAIN_DEADLINE_SECONDS = 5400.0

#: How many releases behind a host must be before it is cordoned. 1 = any
#: drift. See the module docstring's trap-F section for why this is a knob.
DEFAULT_DRIFT_THRESHOLD = 1

#: Returned by :func:`version_drift` when a host's version cannot be compared
#: to the target at all (unreadable lane, or a different minor series). A host
#: whose version we cannot read is NEVER cordoned — cordoning stops real work,
#: and doing that on a guess is the failure this fleet keeps repeating.
DRIFT_UNKNOWN = None

#: The drift reported for a host on a different ``major.minor`` series than
#: the target: larger than any sane threshold, because it genuinely is.
CROSS_SERIES_DRIFT = 9999


def normalize_version(raw: str | None) -> str | None:
    """``v0.5.31`` / ``0.5.31`` -> ``0.5.31``; empty -> ``None``."""
    if not raw:
        return None
    return str(raw).strip().lstrip("vV") or None


def _parts(raw: str | None) -> tuple[int, ...] | None:
    version = normalize_version(raw)
    if not version:
        return None
    out: list[int] = []
    for chunk in version.split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        out.append(int(digits))
    return tuple(out) if out else None


def version_drift(current: str | None, target: str | None) -> int | None:
    """How many releases *current* is behind *target*.

    ``0`` means level or ahead; ``None`` (:data:`DRIFT_UNKNOWN`) means the two
    cannot be compared — an unreadable version, or a target on a different
    ``major.minor`` series, where "how many releases" has no answer from the
    two strings alone.

    Deliberately arithmetic on the patch component rather than a lookup
    against the index: this runs on every propagate tick, and a decision that
    needs a network call to be made is a decision that stops being made the
    moment the network hiccups. A cross-minor gap reports
    :data:`CROSS_SERIES_DRIFT` — "definitely behind, by more than any
    threshold" — which is the only honest reading of ``0.4.x`` vs ``0.5.y``.
    """
    a, b = _parts(current), _parts(target)
    if a is None or b is None:
        return DRIFT_UNKNOWN
    if a >= b:
        return 0
    if a[:2] != b[:2]:
        return CROSS_SERIES_DRIFT
    patch_a = a[2] if len(a) > 2 else 0
    patch_b = b[2] if len(b) > 2 else 0
    return max(0, patch_b - patch_a)


@dataclass(frozen=True)
class Cordon:
    """One machine's release cordon, as stored and as read back.

    ``created_at`` is the moment the host was FIRST cordoned for this drain
    and is preserved across renewals — the drain deadline (trap C) measures
    from it, so a wedged host cannot postpone its own escalation forever by
    being renewed. ``renewed_at``/``expires_at`` move on every renewal.
    """

    machine: str
    owner: str = OWNER_RELEASE
    reason: str = ""
    target_version: str | None = None
    created_at: float = 0.0
    renewed_at: float = 0.0
    expires_at: float = 0.0

    def active(self, now: float) -> bool:
        """Is this record still in force at *now*?

        An ``expires_at`` of 0 (a hand-written record with no expiry) is
        treated as ACTIVE — a cordon nobody can express an expiry for is
        still a cordon — but every record this module writes has one.
        """
        return not self.expires_at or now < self.expires_at

    def expired(self, now: float) -> bool:
        return not self.active(now)

    def age(self, now: float) -> float:
        return max(0.0, now - self.created_at) if self.created_at else 0.0

    def overdue(self, now: float, deadline: float = DEFAULT_DRAIN_DEADLINE_SECONDS) -> bool:
        """Has this host failed to drain within *deadline* seconds?"""
        return bool(self.created_at) and self.age(now) >= deadline > 0

    def describe(self) -> str:
        """The one sentence every surface shows (#2101 trap E).

        Work stopping with no stated reason is the thing this fleet keeps
        doing to itself, so this is deliberately a whole explanation rather
        than a status word: ``cordoned: draining for v0.5.31``.
        """
        if self.target_version:
            return f"cordoned: draining for v{self.target_version}"
        return self.reason or "cordoned: draining for a release"

    def to_dict(self) -> dict:
        return {
            "machine": self.machine,
            "owner": self.owner,
            "reason": self.reason,
            "target_version": self.target_version,
            "created_at": self.created_at,
            "renewed_at": self.renewed_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Cordon":
        """Type one stored record, tolerantly.

        A malformed field degrades to its default rather than raising: this
        is read on every dispatch decision in the fleet, and a cordon store
        nobody can parse must not be able to wedge routing.
        """

        def _float(key: str) -> float:
            try:
                return float(raw.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        target = raw.get("target_version")
        return cls(
            machine=str(raw.get("machine") or ""),
            owner=str(raw.get("owner") or OWNER_RELEASE),
            reason=str(raw.get("reason") or ""),
            target_version=str(target) if target else None,
            created_at=_float("created_at"),
            renewed_at=_float("renewed_at"),
            expires_at=_float("expires_at"),
        )


@dataclass(frozen=True)
class DrainEscalation:
    """A host that has been cordoned longer than the drain deadline.

    Surfaced, never merely recorded: #2101's acceptance criterion 4 asks for
    the *message*, not an internal state change, because a silent forever-wait
    is the failure this whole mechanism is meant to replace.
    """

    machine: str
    waited_seconds: float
    deadline_seconds: float
    target_version: str | None = None
    #: What is still holding the host, in the same prose
    #: ``coord release propagate`` uses for a deferral.
    busy_reason: str = ""

    @property
    def message(self) -> str:
        minutes = self.waited_seconds / 60.0
        limit = self.deadline_seconds / 60.0
        version = f"v{self.target_version}" if self.target_version else "the release"
        holding = f" — still busy: {self.busy_reason}" if self.busy_reason else ""
        return (
            f"DRAIN OVERDUE: {self.machine} has been cordoned for "
            f"{minutes:.0f}m waiting to drain for {version}, past the "
            f"{limit:.0f}m deadline{holding}. New work is NOT being routed "
            f"there. Override with `coord release cordon --clear "
            f"{self.machine}` (which lets work resume and leaves the host "
            f"behind), or clear whatever is wedged and let it drain."
        )

    @property
    def command(self) -> str:
        return f"coord release cordon --clear {self.machine}"

    def to_dict(self) -> dict:
        return {
            "machine": self.machine,
            "waited_seconds": self.waited_seconds,
            "deadline_seconds": self.deadline_seconds,
            "target_version": self.target_version,
            "busy_reason": self.busy_reason,
            "message": self.message,
        }


@dataclass(frozen=True)
class CordonPlan:
    """What one propagate run wants the cordon store to look like.

    Applied by the shell; nothing here writes. ``cordon`` holds both brand-new
    cordons and renewals of existing ones (they are the same write — see
    :class:`Cordon` for why ``created_at`` survives).
    """

    cordon: tuple[Cordon, ...] = ()
    uncordon: tuple[str, ...] = ()
    escalations: tuple[DrainEscalation, ...] = ()
    #: Records that lapsed on their own since the last run (trap B working).
    #: Reported so a self-healed cordon leaves a trace rather than silently
    #: evaporating from the reasoning.
    expired: tuple[str, ...] = ()
    #: Cordoned hosts this run could neither prove current nor prove behind
    #: (unreadable version, or drift under the threshold). Left exactly as
    #: they are — see :func:`plan_cordons` for why neither direction is safe.
    unknown: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.cordon or self.uncordon or self.escalations or self.expired)

    def to_dict(self) -> dict:
        return {
            "cordon": [c.to_dict() for c in self.cordon],
            "uncordon": list(self.uncordon),
            "escalations": [e.to_dict() for e in self.escalations],
            "expired": list(self.expired),
            "unknown": list(self.unknown),
        }

    def render(self) -> list[str]:
        """Human lines for `coord release propagate`'s output."""
        lines: list[str] = []
        for item in self.cordon:
            lines.append(f"  ⊘ cordon {item.machine}: {item.describe()}")
        for name in self.uncordon:
            lines.append(f"  ✓ uncordon {name}: up to date, work may resume")
        for name in self.expired:
            lines.append(
                f"  · cordon on {name} expired on its own (no propagate run "
                "renewed it) — work may resume there"
            )
        for name in self.unknown:
            lines.append(
                f"  ? {name}: cordoned, but this run could neither prove it "
                "current nor prove it behind — left as-is, and it will lapse "
                "on its own if no later run renews it"
            )
        for esc in self.escalations:
            lines.append(f"  ! {esc.message}")
        return lines


@dataclass(frozen=True)
class HostDrift:
    """Which hosts are behind, current, or neither — and why.

    Four buckets, not two, because the two "neither" cases must never be
    collapsed into "current": a host whose version could not be read is not
    evidence of agreement (#1834), and a host one release behind a threshold
    of three is deliberately tolerated rather than proven level.
    """

    behind: frozenset[str] = frozenset()
    current: frozenset[str] = frozenset()
    #: Version unreadable — never cordoned (a cordon on a guess stops real
    #: work) and never uncordoned (an HTTP blip must not open the fleet up
    #: mid-roll).
    unreadable: frozenset[str] = frozenset()
    #: Behind, but by less than the threshold (trap F).
    under_threshold: frozenset[str] = frozenset()

    @property
    def undecided(self) -> frozenset[str]:
        return self.unreadable | self.under_threshold


def classify_hosts(
    host_versions: Mapping[str, str | None],
    target: str | None,
    *,
    threshold: int = DEFAULT_DRIFT_THRESHOLD,
) -> HostDrift:
    """Bucket *host_versions* against *target*. See :class:`HostDrift`.

    *host_versions* maps a machine name to the version its python lane
    reports, ``None`` when no lane could be read. "Current" requires proof:
    an unreadable version is ``unreadable``, never ``current`` — the same rule
    :func:`coord.release_propagate.hosts_already_current` applies, and for
    the same reason (#1834: ``version=None`` means "no data", which is
    emphatically not "agrees with everyone else").
    """
    want = max(1, int(threshold))
    behind: set[str] = set()
    current: set[str] = set()
    unreadable: set[str] = set()
    under: set[str] = set()
    for host, version in host_versions.items():
        drift = version_drift(version, target)
        if drift is DRIFT_UNKNOWN:
            unreadable.add(host)
        elif drift == 0:
            current.add(host)
        elif drift >= want:
            behind.add(host)
        else:
            under.add(host)
    return HostDrift(
        behind=frozenset(behind),
        current=frozenset(current),
        unreadable=frozenset(unreadable),
        under_threshold=frozenset(under),
    )


def plan_cordons(
    *,
    target_version: str | None,
    host_versions: Mapping[str, str | None],
    existing: Mapping[str, Cordon] | Iterable[Cordon] = (),
    now: float,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    drain_deadline: float = DEFAULT_DRAIN_DEADLINE_SECONDS,
    threshold: int = DEFAULT_DRIFT_THRESHOLD,
    busy_reasons: Mapping[str, str] | None = None,
    enabled: bool = True,
) -> CordonPlan:
    """Decide this run's cordon writes. Pure.

    *existing* is the store as read (expired records included — this function
    is what notices they lapsed). *busy_reasons* maps a host to why it is not
    yet drained, purely so an escalation can say what is holding it.

    ``enabled=False`` (``coord release propagate --no-cordon``) plans no new
    cordons but STILL clears the ones this owner already set: turning the
    mechanism off must release the fleet, not freeze it in whatever state the
    last run left behind.
    """
    records = _as_records(existing)
    live = {name: c for name, c in records.items() if c.active(now)}
    expired = tuple(sorted(name for name in records if name not in live))

    drift = classify_hosts(host_versions, target_version, threshold=threshold)

    if not enabled:
        return CordonPlan(uncordon=tuple(sorted(live)), expired=expired)

    to_cordon: list[Cordon] = []
    escalations: list[DrainEscalation] = []
    for host in sorted(drift.behind):
        previous = live.get(host)
        created = previous.created_at if previous and previous.created_at else now
        to_cordon.append(
            Cordon(
                machine=host,
                owner=OWNER_RELEASE,
                reason=f"draining for v{target_version}" if target_version else "draining for a release",
                target_version=target_version,
                created_at=created,
                renewed_at=now,
                expires_at=now + max(0.0, float(ttl_seconds)),
            )
        )
        if previous is not None and previous.overdue(now, drain_deadline):
            escalations.append(
                DrainEscalation(
                    machine=host,
                    waited_seconds=previous.age(now),
                    deadline_seconds=drain_deadline,
                    target_version=target_version,
                    busy_reason=(busy_reasons or {}).get(host, ""),
                )
            )

    # Uncordon: PROVEN current, and nothing else. A host whose version could
    # not be read keeps whatever cordon it has until that cordon EXPIRES —
    # clearing on "we couldn't read the version" would open the fleet up
    # mid-roll on the strength of one failed HTTP call.
    to_uncordon = tuple(sorted(name for name in live if name in drift.current))

    return CordonPlan(
        cordon=tuple(to_cordon),
        uncordon=to_uncordon,
        escalations=tuple(escalations),
        expired=expired,
        unknown=tuple(sorted(drift.undecided & set(live))),
    )


def _as_records(
    existing: Mapping[str, Cordon] | Iterable[Cordon],
) -> dict[str, Cordon]:
    if isinstance(existing, Mapping):
        return {str(k): v for k, v in existing.items()}
    return {c.machine: c for c in existing}


@dataclass
class CordonOutcome:
    """What the shell actually did, for the propagation journal."""

    cordoned: list[str] = field(default_factory=list)
    uncordoned: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    escalated: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "cordoned": list(self.cordoned),
            "uncordoned": list(self.uncordoned),
            "expired": list(self.expired),
            "escalated": [dict(e) for e in self.escalated],
            "errors": list(self.errors),
        }
