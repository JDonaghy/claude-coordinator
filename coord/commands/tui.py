"""`coord tui` — self-update the coord-tui binary from this coordinator's
own GitHub Release channel (#1240, PKG-4).

PKG-3 (#1239) put ``coord-tui-<target>`` binaries on the GitHub Release; the
gap this closes is that a user still had to find the right asset, download
it, `chmod +x` it, and place it by hand. `coord tui update` does all of
that in one command, and `coord tui status` (also what a bare `coord tui`
runs) is the "lightweight version-skew notice" the issue asks for — a
local, on-demand check, not something bolted onto `main()`'s callback and
paid by every `coord` invocation.

The actual resolve/download/install mechanics live in
:mod:`coord.tui_release`, framework-agnostic so they're unit-testable
without going through Click at all; this module is just the CLI wiring.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from coord import __version__
from coord.tui_release import (
    DEFAULT_API_BASE,
    DEFAULT_INSTALL_PATH,
    DEFAULT_REPO,
    DEV_BUILD_SENTINEL_VERSION,
    ReleaseAssetNotFoundError,
    UnsupportedPlatformError,
    detect_target,
    download_asset,
    fetch_release_assets,
    find_asset,
    find_checksum_asset,
    install_atomically,
    read_installed_version,
    sha256_file,
)


def _dest_path(dest: str | None) -> Path:
    return Path(dest).expanduser() if dest else Path(DEFAULT_INSTALL_PATH).expanduser()


@click.group(
    "tui",
    invoke_without_command=True,
    help=(
        "Manage the coord-tui binary. Without a subcommand, runs `coord tui "
        "status` (installed coord-tui version vs. this coordinator's own "
        f"version, {DEFAULT_INSTALL_PATH} by default)."
    ),
)
@click.pass_context
def tui_group(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is None:
        ctx.invoke(tui_status)


@tui_group.command(
    "status",
    help=(
        "Print the installed coord-tui version next to this coordinator's "
        "own version, and hint at `coord tui update` on skew. Purely local "
        "-- reads `<dest> --version`, no network call -- so it's cheap "
        "enough to run on demand without adding it to every `coord` "
        "invocation's hot path."
    ),
)
@click.option(
    "--dest",
    default=None,
    help=f"Path to the coord-tui binary to check. Default: {DEFAULT_INSTALL_PATH}",
)
def tui_status(dest: str | None) -> None:
    _print_skew_notice(_dest_path(dest))


def _print_skew_notice(binary_path: Path) -> bool:
    """Print installed-vs-coordinator version lines. Returns True on skew
    (including "not installed at all"), False when they match."""
    if not binary_path.exists():
        click.echo(f"coord-tui: not installed at {binary_path}")
        click.echo(f"coord:     {__version__}")
        click.echo("Run `coord tui update` to install it.")
        return True

    installed = read_installed_version(binary_path)
    if installed is None:
        click.echo(f"coord-tui: {binary_path} did not report a parseable --version")
        click.echo(f"coord:     {__version__}")
        click.echo("Run `coord tui update` to reinstall it.")
        return True

    click.echo(f"coord-tui: {installed} ({binary_path})")
    click.echo(f"coord:     {__version__}")
    if installed != __version__:
        click.echo(
            "⚠ version skew — run `coord tui update` to match this "
            "coordinator's version."
        )
        return True
    click.echo("✓ up to date")
    return False


@tui_group.command(
    "update",
    help=(
        "Download the coord-tui binary matching this coordinator's own "
        "version (coord --version) from its GitHub Release, and install "
        "it.\n\n"
        "Platform detection: maps this host's `platform.system()`/"
        "`platform.machine()` to one of release-tui.yml's build-matrix "
        "targets (x86_64-linux, x86_64-macos, aarch64-macos, "
        "x86_64-windows) and downloads the matching `coord-tui-<target>` "
        "asset from the GitHub Release tagged v<version>.\n\n"
        f"Install path: {DEFAULT_INSTALL_PATH} by default, override with "
        "--dest. The download always lands in a temp file next to the "
        "destination first -- chmod +x'd and checksum-verified (when the "
        "release publishes one) before an atomic rename into place -- so a "
        "running coord-tui, or an interrupted download, never observes a "
        "partial binary.\n\n"
        "Dev-checkout guard: tui/Cargo.toml's committed [package] version "
        f"is the {DEV_BUILD_SENTINEL_VERSION!r} placeholder that only "
        "release-tui.yml's CI build stamps a real version over (never "
        "committed back) -- so a plain local `cargo build` always reports "
        f"exactly {DEV_BUILD_SENTINEL_VERSION!r}. If the binary already at "
        "--dest reports that sentinel, this refuses to overwrite it "
        "(assuming a developer is iterating on a local build) unless "
        "--force is given."
    ),
)
@click.option(
    "--version",
    "version_override",
    default=None,
    help="Install this exact version instead of this coordinator's own version (coord --version).",
)
@click.option(
    "--dest",
    default=None,
    help=f"Where to install the binary. Default: {DEFAULT_INSTALL_PATH}",
)
@click.option(
    "--repo",
    default=DEFAULT_REPO,
    show_default=True,
    help="GitHub owner/repo the release lives on.",
)
@click.option(
    "--api-base",
    default=DEFAULT_API_BASE,
    show_default=True,
    help="GitHub API base URL -- override to point at a stub endpoint (tests only).",
)
@click.option(
    "--timeout",
    default=30.0,
    show_default=True,
    type=float,
    help="Per-request network timeout (seconds), for both the release lookup and the download.",
)
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Overwrite an installed dev build (see the dev-checkout guard "
        "above), and reinstall even when the destination already reports "
        "the target version."
    ),
)
def tui_update(
    version_override: str | None,
    dest: str | None,
    repo: str,
    api_base: str,
    timeout: float,
    force: bool,  # noqa: FBT001
) -> None:
    target_version = version_override or __version__
    dest_path = _dest_path(dest)

    try:
        target = detect_target()
    except UnsupportedPlatformError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    if dest_path.exists() and not force:
        installed = read_installed_version(dest_path)
        if installed == DEV_BUILD_SENTINEL_VERSION:
            click.echo(
                f"refusing to overwrite {dest_path}: it reports version "
                f"{DEV_BUILD_SENTINEL_VERSION!r}, the sentinel a locally "
                "`cargo build`'d coord-tui always carries (tui/Cargo.toml's "
                "committed version is only stamped for real in CI release "
                "builds). This looks like a dev build someone is iterating "
                "on -- pass --force to overwrite it anyway, or keep "
                "building it yourself: cd tui && cargo build && cp "
                f"target/debug/coord-tui {dest_path}",
                err=True,
            )
            sys.exit(3)
        if installed == target_version:
            click.echo(
                f"coord-tui is already v{target_version} at {dest_path} -- "
                "nothing to do (--force to reinstall)."
            )
            return

    click.echo(f"Detected platform target: {target}")
    click.echo(f"Resolving coord-tui v{target_version} from {repo}...")
    try:
        assets = fetch_release_assets(
            target_version, repo=repo, api_base=api_base, timeout=timeout
        )
        asset = find_asset(assets, target)
    except ReleaseAssetNotFoundError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 -- surface any network/HTTP failure plainly
        click.echo(
            f"error: could not resolve release v{target_version} from {repo}: {exc}",
            err=True,
        )
        sys.exit(1)

    checksum_asset = find_checksum_asset(assets, asset)

    click.echo(f"Downloading {asset.name}...")
    try:
        tmp_path = download_asset(asset.download_url, dest_path.parent, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: download failed: {exc}", err=True)
        sys.exit(1)

    if checksum_asset is not None:
        click.echo(f"Verifying checksum against {checksum_asset.name}...")
        try:
            checksum_tmp = download_asset(
                checksum_asset.download_url, dest_path.parent, timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001
            tmp_path.unlink(missing_ok=True)
            click.echo(
                f"error: could not download checksum {checksum_asset.name}: {exc}",
                err=True,
            )
            sys.exit(1)
        try:
            expected = checksum_tmp.read_text().split()[0].strip()
        finally:
            checksum_tmp.unlink(missing_ok=True)
        actual = sha256_file(tmp_path)
        if actual != expected:
            tmp_path.unlink(missing_ok=True)
            click.echo(
                f"error: checksum mismatch for {asset.name}: expected "
                f"{expected}, got {actual}",
                err=True,
            )
            sys.exit(1)
        click.echo(f"Checksum OK ({actual}).")
    else:
        click.echo(
            "(no published checksum asset for this binary -- PKG-3 does "
            "not currently publish one, so skipping verification)"
        )

    try:
        install_atomically(tmp_path, dest_path)
    except Exception as exc:  # noqa: BLE001
        tmp_path.unlink(missing_ok=True)
        click.echo(f"error: install failed: {exc}", err=True)
        sys.exit(1)

    installed_now = read_installed_version(dest_path)
    click.echo(f"Installed {dest_path} -- coord-tui reports {installed_now or '?'}.")
    if installed_now != target_version:
        click.echo(
            f"⚠ installed binary reports {installed_now!r}, expected "
            f"{target_version!r} -- the release asset may be mislabeled.",
            err=True,
        )
        sys.exit(1)
