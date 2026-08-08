"""Black-box tests for `coord tui update`/`status` (#1240, PKG-4).

PKG-3 (#1239) put `coord-tui-<target>` binaries on the GitHub Release this
project publishes to; PKG-4 is the client half that finds the right asset
for this host's version and platform, downloads it, and installs it without
a human visiting the Releases page by hand.

These tests never touch the real network: a real `http.server.
ThreadingHTTPServer` stands in for GitHub's Releases API and asset CDN on
`127.0.0.1`, and `coord tui update --api-base http://127.0.0.1:<port>`
points at it -- genuinely exercising the HTTP client code path (streamed
download, atomic rename, truncated-connection handling), not a mocked
Python function.
"""

from __future__ import annotations

import http.server
import json
import os
import stat
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import __version__
from coord.cli import main
from coord.tui_release import detect_target

TARGET = detect_target()  # this test host's own platform target, e.g. "x86_64-linux"
ASSET_NAME = f"coord-tui-{TARGET}"
REPO = "acme/coord-tui-test"

# A tiny POSIX shell script standing in for the real Rust binary: mirrors
# `tui/src/main.rs`'s `--version` contract exactly (`coord-tui <version>` on
# stdout, nothing else) so `read_installed_version` parses it the same way
# it would parse the real thing.
_FAKE_BINARY_TEMPLATE = "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then\n  echo 'coord-tui {version}'\nfi\n"


def _fake_binary(version: str) -> bytes:
    return _FAKE_BINARY_TEMPLATE.format(version=version).encode()


class _StubHandler(http.server.BaseHTTPRequestHandler):
    """Reads test fixtures off the class itself (set per-server-instance
    below) rather than per-request state -- there's exactly one client per
    test here, so this stays simple."""

    def log_message(self, *args) -> None:  # noqa: D401 -- silence test-run noise
        pass

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's own naming
        routes: dict = self.server.routes  # type: ignore[attr-defined]
        route = routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return

        kind, payload = route
        if kind == "json":
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif kind == "bytes":
            body = payload
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif kind == "truncated":
            # Declares a body far larger than what it actually sends, then
            # closes the connection -- the "interrupted download" case.
            declared_len, actual_body = payload
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(declared_len))
            self.end_headers()
            self.wfile.write(actual_body)
            self.close_connection = True
        else:  # pragma: no cover -- test-authoring error, not a runtime path
            raise AssertionError(f"unknown route kind {kind!r}")


@pytest.fixture
def stub_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    server.routes = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _api_base(server) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


def _release_route(version: str, assets: list[dict]) -> tuple[str, dict]:
    return f"/repos/{REPO}/releases/tags/v{version}", {"tag_name": f"v{version}", "assets": assets}


# ── happy path ────────────────────────────────────────────────────────────


def test_tui_update_downloads_verifies_and_installs_executable(stub_server, tmp_path: Path) -> None:
    version = __version__
    body = _fake_binary(version)
    server = stub_server
    path, payload = _release_route(
        version,
        [{"name": ASSET_NAME, "browser_download_url": f"{_api_base(server)}/download/{ASSET_NAME}"}],
    )
    server.routes[path] = ("json", payload)
    server.routes[f"/download/{ASSET_NAME}"] = ("bytes", body)

    dest = tmp_path / "bin" / "coord-tui"
    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
            "--timeout", "5",
        ],
    )

    assert result.exit_code == 0, result.output
    assert dest.exists()
    mode = dest.stat().st_mode
    assert mode & stat.S_IXUSR, "installed binary must be chmod +x"

    # The install path reports the expected version -- proves the atomic
    # rename landed the actual downloaded bytes at `dest`, not a stub.
    out = os.popen(f"{dest} --version").read().strip()
    assert out == f"coord-tui {version}"

    # No leftover temp file from the download step.
    leftovers = list(dest.parent.glob(".coord-tui-download-*"))
    assert leftovers == [], f"download temp file(s) not cleaned up: {leftovers}"


def test_tui_update_is_idempotent_when_already_current(stub_server, tmp_path: Path) -> None:
    """Re-running against an install that already reports the target
    version is a no-op (no network hit) unless --force is given."""
    version = __version__
    dest = tmp_path / "bin" / "coord-tui"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(_fake_binary(version))
    dest.chmod(dest.stat().st_mode | 0o111)

    server = stub_server
    # Deliberately no routes registered -- a network hit here would 404 and
    # fail the command, proving the "already current" short-circuit fired
    # before any HTTP call.
    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "nothing to do" in result.output


# ── interrupted download / atomic rename ───────────────────────────────────


def test_tui_update_interrupted_download_leaves_no_partial_file(stub_server, tmp_path: Path) -> None:
    version = __version__
    server = stub_server
    path, payload = _release_route(
        version,
        [{"name": ASSET_NAME, "browser_download_url": f"{_api_base(server)}/download/{ASSET_NAME}"}],
    )
    server.routes[path] = ("json", payload)
    # Declares 10x the body it actually sends, then drops the connection.
    server.routes[f"/download/{ASSET_NAME}"] = ("truncated", (5000, b"only-a-few-bytes"))

    dest = tmp_path / "bin" / "coord-tui"
    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
            "--timeout", "5",
        ],
    )

    assert result.exit_code != 0, result.output
    assert not dest.exists(), "an interrupted download must never land at the destination"
    assert not dest.parent.exists() or list(dest.parent.glob(".coord-tui-download-*")) == [], (
        "an interrupted download must not leave a stray temp file behind"
    )


def test_tui_update_missing_asset_reports_error(stub_server, tmp_path: Path) -> None:
    version = __version__
    server = stub_server
    path, payload = _release_route(version, [{"name": "coord-tui-some-other-target", "browser_download_url": "x"}])
    server.routes[path] = ("json", payload)

    dest = tmp_path / "bin" / "coord-tui"
    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
            "--timeout", "5",
        ],
    )

    assert result.exit_code != 0
    assert not dest.exists()
    assert ASSET_NAME in result.output


# ── checksum verification, when the release publishes one ─────────────────


def test_tui_update_verifies_published_checksum(stub_server, tmp_path: Path) -> None:
    import hashlib

    version = __version__
    body = _fake_binary(version)
    digest = hashlib.sha256(body).hexdigest()
    server = stub_server
    path, payload = _release_route(
        version,
        [
            {"name": ASSET_NAME, "browser_download_url": f"{_api_base(server)}/download/{ASSET_NAME}"},
            {
                "name": f"{ASSET_NAME}.sha256",
                "browser_download_url": f"{_api_base(server)}/download/{ASSET_NAME}.sha256",
            },
        ],
    )
    server.routes[path] = ("json", payload)
    server.routes[f"/download/{ASSET_NAME}"] = ("bytes", body)
    server.routes[f"/download/{ASSET_NAME}.sha256"] = ("bytes", f"{digest}  {ASSET_NAME}\n".encode())

    dest = tmp_path / "bin" / "coord-tui"
    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
            "--timeout", "5",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Checksum OK" in result.output
    assert dest.exists()


def test_tui_update_rejects_mismatched_checksum(stub_server, tmp_path: Path) -> None:
    version = __version__
    body = _fake_binary(version)
    server = stub_server
    path, payload = _release_route(
        version,
        [
            {"name": ASSET_NAME, "browser_download_url": f"{_api_base(server)}/download/{ASSET_NAME}"},
            {
                "name": f"{ASSET_NAME}.sha256",
                "browser_download_url": f"{_api_base(server)}/download/{ASSET_NAME}.sha256",
            },
        ],
    )
    server.routes[path] = ("json", payload)
    server.routes[f"/download/{ASSET_NAME}"] = ("bytes", body)
    server.routes[f"/download/{ASSET_NAME}.sha256"] = ("bytes", b"0" * 64 + f"  {ASSET_NAME}\n".encode())

    dest = tmp_path / "bin" / "coord-tui"
    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
            "--timeout", "5",
        ],
    )

    assert result.exit_code != 0
    assert not dest.exists()
    assert "checksum mismatch" in result.output


# ── dev-checkout guard ──────────────────────────────────────────────────────


def test_tui_update_refuses_to_clobber_dev_build_without_force(stub_server, tmp_path: Path) -> None:
    from coord.tui_release import DEV_BUILD_SENTINEL_VERSION

    dest = tmp_path / "bin" / "coord-tui"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(_fake_binary(DEV_BUILD_SENTINEL_VERSION))
    dest.chmod(dest.stat().st_mode | 0o111)

    server = stub_server
    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
        ],
    )

    assert result.exit_code == 3, result.output
    assert "refusing to overwrite" in result.output
    # Untouched -- still the dev build, not replaced or corrupted.
    assert dest.read_bytes() == _fake_binary(DEV_BUILD_SENTINEL_VERSION)


def test_tui_update_force_overwrites_dev_build(stub_server, tmp_path: Path) -> None:
    from coord.tui_release import DEV_BUILD_SENTINEL_VERSION

    version = __version__
    dest = tmp_path / "bin" / "coord-tui"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(_fake_binary(DEV_BUILD_SENTINEL_VERSION))
    dest.chmod(dest.stat().st_mode | 0o111)

    server = stub_server
    path, payload = _release_route(
        version,
        [{"name": ASSET_NAME, "browser_download_url": f"{_api_base(server)}/download/{ASSET_NAME}"}],
    )
    server.routes[path] = ("json", payload)
    server.routes[f"/download/{ASSET_NAME}"] = ("bytes", _fake_binary(version))

    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
            "--force",
            "--timeout", "5",
        ],
    )

    assert result.exit_code == 0, result.output
    out = os.popen(f"{dest} --version").read().strip()
    assert out == f"coord-tui {version}"


# ── version-skew notice (`coord tui status`, and bare `coord tui`) ─────────


def test_tui_status_reports_skew() -> None:
    with CliRunner().isolated_filesystem():
        dest = Path("coord-tui")
        dest.write_bytes(_fake_binary("0.0.1-not-the-real-version"))
        dest.chmod(dest.stat().st_mode | 0o111)

        result = CliRunner().invoke(main, ["tui", "status", "--dest", str(dest)])

        assert result.exit_code == 0, result.output
        assert "version skew" in result.output
        assert __version__ in result.output


def test_tui_status_reports_up_to_date() -> None:
    with CliRunner().isolated_filesystem():
        dest = Path("coord-tui")
        dest.write_bytes(_fake_binary(__version__))
        dest.chmod(dest.stat().st_mode | 0o111)

        result = CliRunner().invoke(main, ["tui", "status", "--dest", str(dest)])

        assert result.exit_code == 0, result.output
        assert "up to date" in result.output


def test_bare_tui_command_runs_status(tmp_path: Path) -> None:
    dest = tmp_path / "coord-tui"
    result = CliRunner().invoke(main, ["tui", "--dest", str(dest)])
    # `--dest` belongs to the `status` subcommand, not the group itself --
    # confirm the bare `coord tui` really does dispatch into `status`
    # (which reports "not installed" here) rather than silently no-op'ing.
    assert result.exit_code != 0  # unknown option at the group level

    result = CliRunner().invoke(main, ["tui"])
    assert result.exit_code == 0, result.output
    assert "coord-tui" in result.output


# ── --help documents platform detection + install path (acceptance) ────────


def test_tui_update_help_documents_platform_and_install_path() -> None:
    result = CliRunner().invoke(main, ["tui", "update", "--help"])
    assert result.exit_code == 0
    assert "x86_64-linux" in result.output
    assert "x86_64-macos" in result.output
    assert "aarch64-macos" in result.output
    assert "x86_64-windows" in result.output
    assert "~/.local/bin/coord-tui" in result.output
