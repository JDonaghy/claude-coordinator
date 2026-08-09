#!/usr/bin/env python3
"""Prove a release *actually happened* before the release job reports success.

#2035: PKG-7's merge-triggered release pushed ``refs/tags/v0.5.2``, published
nothing, and went green.  The trigger bug (a tag pushed with the default
``GITHUB_TOKEN`` does not start a workflow — GitHub's recursion guard) is
fixed by calling ``publish.yml`` directly, but the *reason it cost a day* is
separate and outlives any one trigger: **the release path could not detect
its own no-op.**  A workflow whose only evidence of success is "none of my
steps exited nonzero" reports success for a release that produced no wheel,
no Release, and no artifact anyone can install.

Downstream that is worse than a red build.  ``coord agent update`` finds no
0.5.2 on the index and stays on 0.5.1; the daemon host stays on 0.5.1;
``coord release verify`` sees every lane agreeing on 0.5.1 and reports
all-green while ``main`` has moved on.  The fleet ends up consistent and
consistently stale — exactly the state the release tooling exists to make
impossible.

So this script is the post-condition, and it is deliberately independent of
*how* the release was triggered:

1. **The version is resolvable by pip.**  Polls the PEP 503 **simple index**
   — not the JSON API.  They flip independently in both directions (see
   ``coord/health/pypi.py``'s module docstring, #1628), and only the simple
   index is what ``pip install`` resolves against.  A check that passes
   against the JSON API while ``pip`` still can't see the version is a check
   that certifies the exact failure it exists to catch.
2. **The GitHub Release exists** for the tag (optional; needs a token).

Both are polled with a timeout rather than probed once: PyPI's CDN takes
seconds-to-minutes to serve a fresh upload, and a single immediate probe
would flap.  On timeout the job **fails** and says plainly which artifact was
missing — the whole point being that a release which silently did nothing
must not be able to end in a green check mark.

Kept as a standalone script (like ``scripts/verify_release_wheel.py``) so
``tests/test_auto_release_publish_2035.py`` can drive it with an injected
fetcher and clock instead of a GitHub Actions runner and a real PyPI.

Usage::

    verify_release_published.py --tag v0.5.2 --github-repo owner/repo

Exit status is 0 only when every requested artifact was observed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT))

# Reuse the fleet-health index parser rather than writing a second one: it
# already handles PEP 503 normalisation, wheel-vs-sdist filename splitting and
# `data-yanked` exclusion, and it is pure stdlib so a bare CI runner can
# import it with no `pip install`.  Two parsers for one index is how the two
# halves of a release check end up disagreeing about what is published.
from coord.health.pypi import (  # noqa: E402 - needs the sys.path line above
    normalize_name,
    parse_simple_index,
    parse_version,
)

#: PyPI's simple index.  Overridable so tests never touch the network and an
#: operator can point at a devpi/mirror.
DEFAULT_INDEX_URL = "https://pypi.org/simple"

#: Long enough for PyPI's CDN to serve a fresh upload on a bad day, short
#: enough that a genuinely-missing release fails the job inside one coffee.
DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_INTERVAL_SECONDS = 15.0

#: A yanked release is excluded by :func:`parse_simple_index`, and rightly so:
#: ``pip`` will not resolve to one, so "published" would be a lie.


class VerificationError(Exception):
    """A post-condition of the release does not hold."""


@dataclass(frozen=True)
class PollOutcome:
    """What a poll loop observed, whether or not it succeeded."""

    ok: bool
    detail: str
    attempts: int
    waited: float


# ──────────────────────────────────────────────────────────────────────────
# pure helpers
# ──────────────────────────────────────────────────────────────────────────


def version_from_tag(tag: str) -> str:
    """``v0.5.2`` -> ``0.5.2``.  A bare ``0.5.2`` is accepted unchanged."""
    tag = tag.strip()
    if not tag:
        raise VerificationError("empty release tag")
    return tag[1:] if tag[0] in "vV" else tag


def index_url_for(package: str, index_url: str = DEFAULT_INDEX_URL) -> str:
    return f"{index_url.rstrip('/')}/{normalize_name(package)}/"


def index_has_version(html: str, package: str, version: str) -> bool:
    """Is *version* servable from this simple-index page?

    Comparison is on the parsed version, not the literal string, so an index
    spelling of ``0.5.2`` matches a tag of ``v0.5.2`` and ``1.0`` matches
    ``1.0.0`` — the same equivalence :func:`parse_version` already uses for
    "how many releases behind am I".
    """
    wanted = parse_version(version)
    published = parse_simple_index(html, package)
    if wanted is None:
        # Unparseable version (shouldn't happen for a `vX.Y.Z` tag): fall back
        # to an exact string match rather than silently passing.
        return any(v.raw == version.strip() for v in published)
    return any(v == wanted for v in published)


def package_name_from_pyproject(path: Path | None = None) -> str:
    """``[project] name`` from pyproject.toml.

    The workflow does not hardcode the distribution name for the same reason
    nothing hardcodes the version (#1238): a renamed project must not leave a
    post-condition quietly polling the old index page forever, which would
    fail every release instead of catching a broken one.
    """
    import tomllib  # noqa: PLC0415 - stdlib since 3.11, only needed here

    path = path or (REPO_ROOT / "pyproject.toml")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    name = (data.get("project") or {}).get("name")
    if not name:
        raise VerificationError(f"{path} declares no [project] name")
    return str(name)


# ──────────────────────────────────────────────────────────────────────────
# fetchers (injected in tests)
# ──────────────────────────────────────────────────────────────────────────

#: ``url -> body`` or ``None`` when the resource does not exist / is not yet
#: servable.  Anything other than "definitely absent" should raise, so a
#: transient 503 is retried rather than mistaken for a missing release.
Fetcher = Callable[[str], "str | None"]


def http_get(url: str, *, headers: dict[str, str] | None = None, timeout: float = 20.0) -> str | None:
    """GET *url*, returning ``None`` for 404 and raising for anything else."""
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https scheme
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def make_index_fetcher(timeout: float = 20.0) -> Fetcher:
    def fetch(url: str) -> str | None:
        return http_get(url, headers={"Accept": "text/html"}, timeout=timeout)

    return fetch


def make_github_fetcher(token: str | None, timeout: float = 20.0) -> Fetcher:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def fetch(url: str) -> str | None:
        return http_get(url, headers=headers, timeout=timeout)

    return fetch


# ──────────────────────────────────────────────────────────────────────────
# poll loops
# ──────────────────────────────────────────────────────────────────────────


def _poll(
    probe: Callable[[], tuple[bool, str]],
    *,
    timeout: float,
    interval: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    log: Callable[[str], None],
) -> PollOutcome:
    """Run *probe* until it reports success or *timeout* elapses.

    Always probes at least once even with ``timeout=0``, so a caller that just
    wants a single check does not have to special-case the loop.
    """
    started = monotonic()
    attempts = 0
    detail = "not attempted"
    while True:
        attempts += 1
        try:
            ok, detail = probe()
        except Exception as exc:  # noqa: BLE001 - a transient fetch error is a retry, not a verdict
            ok, detail = False, f"probe error: {exc}"
        waited = monotonic() - started
        if ok:
            return PollOutcome(True, detail, attempts, waited)
        if waited + interval > timeout:
            return PollOutcome(False, detail, attempts, waited)
        log(f"  ...{detail}; retrying in {interval:.0f}s ({waited:.0f}s/{timeout:.0f}s elapsed)")
        sleep(interval)


def wait_for_pypi(
    package: str,
    version: str,
    *,
    index_url: str = DEFAULT_INDEX_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    fetch: Fetcher | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> PollOutcome:
    """Poll the simple index until *version* is servable."""
    fetch = fetch or make_index_fetcher()
    url = index_url_for(package, index_url)

    def probe() -> tuple[bool, str]:
        html = fetch(url)
        if html is None:
            return False, f"{url} 404s — no such project on the index"
        if index_has_version(html, package, version):
            return True, f"{package} {version} is servable from {url}"
        seen = [v.raw for v in parse_simple_index(html, package)]
        newest = ", ".join(seen[-3:]) if seen else "(none)"
        return False, f"{package} {version} not on {url} (newest there: {newest})"

    return _poll(probe, timeout=timeout, interval=interval, monotonic=monotonic, sleep=sleep, log=log)


def wait_for_github_release(
    repo: str,
    tag: str,
    *,
    api_url: str = "https://api.github.com",
    timeout: float = 120.0,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    fetch: Fetcher | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> PollOutcome:
    """Poll the GitHub API until a Release exists for *tag*."""
    fetch = fetch or make_github_fetcher(os.environ.get("GITHUB_TOKEN"))
    url = f"{api_url.rstrip('/')}/repos/{repo}/releases/tags/{tag}"

    def probe() -> tuple[bool, str]:
        body = fetch(url)
        if body is None:
            return False, f"no GitHub Release for {tag} in {repo}"
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            return False, f"unparseable release payload for {tag}: {exc}"
        if payload.get("draft"):
            return False, f"the Release for {tag} is still a draft"
        assets = payload.get("assets") or []
        return True, f"GitHub Release {tag} exists with {len(assets)} asset(s)"

    return _poll(probe, timeout=timeout, interval=interval, monotonic=monotonic, sleep=sleep, log=log)


# ──────────────────────────────────────────────────────────────────────────
# entrypoint
# ──────────────────────────────────────────────────────────────────────────


def format_failure(tag: str, missing: list[str]) -> str:
    """The message the operator reads when a 'successful' release shipped nothing."""
    bullets = "\n".join(f"  - {item}" for item in missing)
    return (
        f"Release {tag} reported success but the artifacts do not exist:\n"
        f"{bullets}\n"
        "This is #2035: a release path that cannot detect its own no-op. Do NOT "
        "treat the tag as released — every deploy lane would target a version "
        "that is not installable, and uniform staleness reads as health."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tag", required=True, help="the release tag, e.g. v0.5.2")
    parser.add_argument(
        "--package",
        default=None,
        help="distribution name (default: [project] name from pyproject.toml)",
    )
    parser.add_argument("--index-url", default=DEFAULT_INDEX_URL)
    parser.add_argument(
        "--github-repo",
        default=None,
        help="owner/repo; when given, also require a GitHub Release for the tag",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument(
        "--skip-pypi",
        action="store_true",
        help="only check the GitHub Release (for a release that publishes no wheel)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tag = args.tag.strip()
    version = version_from_tag(tag)
    package = args.package or package_name_from_pyproject()

    missing: list[str] = []

    if not args.skip_pypi:
        print(f"Waiting for {package} {version} on the PyPI simple index...")
        outcome = wait_for_pypi(
            package,
            version,
            index_url=args.index_url,
            timeout=args.timeout,
            interval=args.interval,
        )
        print(f"  {'OK' if outcome.ok else 'MISSING'}: {outcome.detail}")
        if not outcome.ok:
            missing.append(
                f"PyPI simple index: {outcome.detail} "
                f"(gave up after {outcome.attempts} probes over {outcome.waited:.0f}s)"
            )

    if args.github_repo:
        print(f"Waiting for the GitHub Release {tag} in {args.github_repo}...")
        outcome = wait_for_github_release(
            args.github_repo,
            tag,
            timeout=min(args.timeout, 120.0),
            interval=args.interval,
        )
        print(f"  {'OK' if outcome.ok else 'MISSING'}: {outcome.detail}")
        if not outcome.ok:
            missing.append(f"GitHub Release: {outcome.detail}")

    if missing:
        message = format_failure(tag, missing)
        # `::error::` needs one line to render in the Actions annotation UI.
        print(f"::error::{message}".replace("\n", " "), file=sys.stderr)
        print(message, file=sys.stderr)
        return 1

    print(f"Release {tag} is real: every requested artifact is present.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
