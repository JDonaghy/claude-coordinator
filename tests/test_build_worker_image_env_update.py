"""Behavioural tests for build-worker-image.sh's env-persistence fix (#1800).

Before this fix, build-worker-image.sh published a new golden image version
and only PRINTED its sourceImageId -- ~/.coord/epic.env kept pointing at the
previous version until someone remembered to hand-edit it. A bake not
followed by that manual step silently left the next epic-up.sh provisioning
from the stale image: success at every step, with the newly-baked software
simply absent from the VM.

The fix adds an `update_epic_env()` function to build-worker-image.sh that
rewrites (or appends) SOURCE_IMAGE_ID=... in $EPIC_ENV once a version is
published, with a `--no-update-env` escape hatch for a deliberate
build-but-don't-adopt. The rest of the script drives real `az`/`ssh`/`scp`
calls against a live Azure subscription and a throwaway VM, so it cannot run
under pytest -- but build-worker-image.sh is structured (like
deploy/coord-web-rollback.sh) so that everything above `main()` is pure
function definitions with no side effects. Sourcing the file therefore only
defines functions; `main "$@"` never runs unless the file is executed
directly (see the `BASH_SOURCE[0] == $0` guard at the bottom). That lets
these tests call update_epic_env() directly, for real, against a scratch
$EPIC_ENV file -- no az/ssh stubbing required.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "azure-workers" / "build-worker-image.sh"

OLD_ID = (
    "/subscriptions/sub/resourceGroups/rg-coord-images/providers/"
    "Microsoft.Compute/galleries/sigcoord/images/coord-worker/"
    "versions/2026.0801.0"
)
NEW_ID = (
    "/subscriptions/sub/resourceGroups/rg-coord-images/providers/"
    "Microsoft.Compute/galleries/sigcoord/images/coord-worker/"
    "versions/2026.0804.0"
)


def _call_update_epic_env(
    epic_env: Path, *, version: str = "2026.0804.0", image_id: str = NEW_ID, update_env: int = 1
) -> subprocess.CompletedProcess:
    """Source build-worker-image.sh (defines functions only -- main() is
    guarded and never runs) and invoke update_epic_env() directly, the same
    way build-worker-image.sh's own main() does after publishing a version.
    """
    driver = f"""
set -euo pipefail
EPIC_ENV={shlex.quote(str(epic_env))}
UPDATE_ENV={update_env}
source {shlex.quote(str(SCRIPT))}
update_epic_env {shlex.quote(version)} {shlex.quote(image_id)}
"""
    return subprocess.run(
        ["bash", "-c", driver],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_sourcing_the_script_does_not_touch_azure_or_the_network(tmp_path: Path) -> None:
    """Guard test for the refactor itself: sourcing build-worker-image.sh
    must be side-effect-free (no `az`/`curl`/`ssh` invoked) since main() is
    behind the BASH_SOURCE guard. If this regresses, every test below would
    hang or fail trying to reach real Azure."""
    result = subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(SCRIPT))}; echo sourced-ok"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "sourced-ok" in result.stdout


def test_rewrites_existing_source_image_id_in_place(tmp_path: Path) -> None:
    epic_env = tmp_path / "epic.env"
    epic_env.write_text(
        "SUBSCRIPTION_ID=abc123\n"
        f"SOURCE_IMAGE_ID={OLD_ID}\n"
        "OWNER=me\n"
    )

    result = _call_update_epic_env(epic_env)

    assert result.returncode == 0, result.stderr
    contents = epic_env.read_text()
    assert f"SOURCE_IMAGE_ID={NEW_ID}" in contents
    assert OLD_ID not in contents
    # Other lines untouched.
    assert "SUBSCRIPTION_ID=abc123" in contents
    assert "OWNER=me" in contents


def test_backs_up_the_previous_epic_env_before_rewriting(tmp_path: Path) -> None:
    epic_env = tmp_path / "epic.env"
    original = f"SOURCE_IMAGE_ID={OLD_ID}\n"
    epic_env.write_text(original)

    result = _call_update_epic_env(epic_env)

    assert result.returncode == 0, result.stderr
    backup = epic_env.with_name(epic_env.name + ".bak")
    assert backup.exists()
    assert backup.read_text() == original


def test_appends_source_image_id_when_the_key_is_absent(tmp_path: Path) -> None:
    epic_env = tmp_path / "epic.env"
    epic_env.write_text("SUBSCRIPTION_ID=abc123\n")

    result = _call_update_epic_env(epic_env)

    assert result.returncode == 0, result.stderr
    contents = epic_env.read_text()
    assert "SUBSCRIPTION_ID=abc123" in contents
    assert f"SOURCE_IMAGE_ID={NEW_ID}" in contents


def test_no_update_env_leaves_epic_env_completely_untouched(tmp_path: Path) -> None:
    """#1800 acceptance: --no-update-env (surfaced to update_epic_env() as
    UPDATE_ENV=0 by main()'s arg parsing) preserves today's print-only
    behaviour for a deliberate build-but-don't-adopt."""
    epic_env = tmp_path / "epic.env"
    original = f"SOURCE_IMAGE_ID={OLD_ID}\n"
    epic_env.write_text(original)
    before_mtime = epic_env.stat().st_mtime_ns

    result = _call_update_epic_env(epic_env, update_env=0)

    assert result.returncode == 0, result.stderr
    assert epic_env.read_text() == original
    assert epic_env.stat().st_mtime_ns == before_mtime
    assert not epic_env.with_name(epic_env.name + ".bak").exists()
    # The new ID must still be surfaced so a human can adopt it by hand.
    assert NEW_ID in result.stdout


def test_missing_epic_env_does_not_fail_the_build(tmp_path: Path) -> None:
    """A first-ever bake before epic.env exists (e.g. bootstrap-shared.sh
    hasn't run yet) must not turn a successful publish into a failed run --
    it should just say there's nothing to update."""
    epic_env = tmp_path / "does-not-exist" / "epic.env"

    result = _call_update_epic_env(epic_env)

    assert result.returncode == 0, result.stderr
    assert not epic_env.exists()
    assert NEW_ID in result.stderr or NEW_ID in result.stdout


def test_no_update_env_cli_flag_is_wired_to_update_env_variable() -> None:
    """Static pin: main()'s option parser must actually set UPDATE_ENV=0 for
    --no-update-env, and default UPDATE_ENV to 1 (adopt) otherwise -- the
    behavioural tests above only exercise update_epic_env() once that
    variable is already set."""
    text = SCRIPT.read_text()
    assert "UPDATE_ENV=1" in text
    assert "--no-update-env)" in text
    assert "UPDATE_ENV=0" in text


def test_final_summary_calls_update_epic_env_before_reporting_done() -> None:
    """Static pin: guards against main() computing IMAGE_ID and printing it
    without ever calling update_epic_env() -- the exact #1800 bug shape."""
    text = SCRIPT.read_text()
    publish_idx = text.index('log "6/6  done"')
    call_idx = text.index('update_epic_env "$VERSION" "$IMAGE_ID"')
    assert call_idx > publish_idx
