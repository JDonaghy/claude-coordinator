"""Behavioural tests for epic-up.sh's image-provenance reporting (#1800).

The rest of #1800's fix lives in build-worker-image.sh (see
tests/test_build_worker_image_env_update.py). This half covers the fallback
epic-up.sh itself must provide per the issue's acceptance criteria: even
with SOURCE_IMAGE_ID auto-updated, an operator staring at epic-up.sh output
should be able to see which image version and publish date it deployed
from, and get a loud warning if that pin is not the newest version in the
gallery (a deliberate pin is legitimate; an accidental stale one should not
be silent).

epic-up.sh drives real `az`/`ssh`/`curl`/coordinator.yml-editing calls
against a live subscription and daemon host, so the whole script cannot run
under pytest. It is structured the same way as build-worker-image.sh
(#1800's fix): everything above `main()` -- parse_image_id() and
report_image_provenance() -- is pure or `az`-calling function definitions
with no side effects at source time, guarded behind a `BASH_SOURCE[0] ==
$0` check so sourcing the file never runs main(). These tests source the
file and call those two functions directly, stubbing `az` as a bash
function (inherited into command-substitution subshells for free, since
`$(...)` forks the same interpreter rather than spawning a new bash).
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "azure-workers" / "epic-up.sh"

CURRENT_ID = (
    "/subscriptions/sub/resourceGroups/rg-coord-images/providers/"
    "Microsoft.Compute/galleries/sigcoord/images/coord-worker/"
    "versions/2026.0801.0"
)


def _run(body: str) -> subprocess.CompletedProcess:
    driver = f"""
set -euo pipefail
source {shlex.quote(str(SCRIPT))}
{body}
"""
    return subprocess.run(
        ["bash", "-c", driver],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_sourcing_the_script_does_not_touch_azure_or_the_network() -> None:
    """Guard test for the refactor itself, mirroring the equivalent check in
    test_build_worker_image_env_update.py: sourcing epic-up.sh must be
    side-effect-free since main() is behind the BASH_SOURCE guard."""
    result = _run("echo sourced-ok")
    assert result.returncode == 0, result.stderr
    assert "sourced-ok" in result.stdout


def test_parse_image_id_extracts_version_from_a_gallery_resource_id() -> None:
    result = _run(f'parse_image_id {shlex.quote(CURRENT_ID)} version')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2026.0801.0"


def test_parse_image_id_extracts_gallery_and_definition() -> None:
    result = _run(
        f'parse_image_id {shlex.quote(CURRENT_ID)} gallery; '
        f'parse_image_id {shlex.quote(CURRENT_ID)} image; '
        f'parse_image_id {shlex.quote(CURRENT_ID)} rg'
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines == ["sigcoord", "coord-worker", "rg-coord-images"]


def _fake_az_newest(newest: str) -> str:
    """A bash `az` stand-in covering the two calls report_image_provenance()
    makes: `sig image-version show --ids ... publishedDate` and
    `sig image-version list ...`. Matches on the joined argv the same way a
    human would read the real command."""
    return f"""
az() {{
    local args="$*"
    case "$args" in
        *"image-version show --ids"*"publishedDate"*)
            echo "2026-08-01T12:00:00+00:00" ;;
        *"image-version list"*)
            printf '2026.0801.0\\n{newest}\\n' ;;
        *)
            echo "unexpected az call: $args" >&2; return 1 ;;
    esac
}}
"""


def test_report_image_provenance_prints_version_and_publish_date() -> None:
    result = _run(
        _fake_az_newest("2026.0801.0")
        + f'report_image_provenance {shlex.quote(CURRENT_ID)}'
    )
    assert result.returncode == 0, result.stderr
    assert "2026.0801.0" in result.stdout
    assert "2026-08-01" in result.stdout


def test_report_image_provenance_warns_when_pin_is_not_the_newest_gallery_version() -> None:
    """#1800 acceptance: a SOURCE_IMAGE_ID older than the newest gallery
    version produces a visible warning."""
    result = _run(
        _fake_az_newest("2026.0804.0")
        + f'report_image_provenance {shlex.quote(CURRENT_ID)}'
    )
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr
    assert "2026.0801.0" in result.stderr
    assert "2026.0804.0" in result.stderr


def test_report_image_provenance_is_silent_when_pin_is_already_newest() -> None:
    result = _run(
        _fake_az_newest("2026.0801.0")
        + f'report_image_provenance {shlex.quote(CURRENT_ID)}'
    )
    assert result.returncode == 0, result.stderr
    assert "WARNING" not in result.stderr


def test_deploy_step_calls_report_image_provenance_before_creating_the_resource_group() -> None:
    """Static pin: guards against main() deploying without ever surfacing
    the image version/publish date -- the exact half of #1800 this file
    covers."""
    text = SCRIPT.read_text()
    deploy_idx = text.index('log "1/5  deploy $RG"')
    report_idx = text.index('report_image_provenance "$SOURCE_IMAGE_ID"')
    group_create_idx = text.index('az group create -n "$RG"')
    assert deploy_idx < report_idx < group_create_idx


def test_final_summary_names_the_image_version_deployed() -> None:
    """#1800 acceptance: epic-up.sh output names the image version it
    deployed -- assertable from the ready-summary heredoc."""
    text = SCRIPT.read_text()
    assert 'parse_image_id "$SOURCE_IMAGE_ID" version' in text
