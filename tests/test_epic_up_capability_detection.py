"""Tests for epic-up.sh's capability-detection path (#1799).

#1799: `epic-up.sh` hardcoded `CAPABILITIES="rust,python"`, which was correct
before #1777 but became wrong the moment the golden image started shipping
opencode — the machine it just provisioned could run opencode but never
said so, so `coord assign --provider oc-*` refused it with "does not
advertise 'provider:opencode'". Hardcoding `provider:opencode` into the
default instead would just trade one drift-prone constant for another (the
image is what actually determines truth, not a flag written the day the
image last changed). So epic-up.sh now asks the freshly-provisioned machine
directly (`ssh <machine> 'command -v opencode'`) and only advertises the
capability when it's actually there.

Same harness shape as test_epic_up_image_provenance.py: source the file
(guarded behind `BASH_SOURCE[0] == $0`, so sourcing never runs main()) and
call the pure/ssh-calling functions directly, stubbing `ssh` as a bash
function.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "azure-workers" / "epic-up.sh"


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
    """Guard test mirroring test_epic_up_image_provenance.py: sourcing must
    stay side-effect-free (main() is behind the BASH_SOURCE guard) even
    after adding the #1799 detection functions above it."""
    result = _run("echo sourced-ok")
    assert result.returncode == 0, result.stderr
    assert "sourced-ok" in result.stdout


# --- add_capability_if_missing: pure string logic, no stubbing needed ------


def test_add_capability_if_missing_appends_when_absent() -> None:
    result = _run('add_capability_if_missing "rust,python" "provider:opencode"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "rust,python,provider:opencode"


def test_add_capability_if_missing_is_idempotent() -> None:
    result = _run(
        'add_capability_if_missing "rust,python,provider:opencode" "provider:opencode"'
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "rust,python,provider:opencode"


def test_add_capability_if_missing_handles_empty_csv() -> None:
    result = _run('add_capability_if_missing "" "provider:opencode"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "provider:opencode"


# --- detect_opencode_capability: stub ssh, same pattern as az above --------


def _fake_ssh(reachable: bool, has_opencode: bool) -> str:
    """A bash `ssh` stand-in for `detect_opencode_capability`'s one call:
    `ssh -o ... <machine> 'command -v opencode >/dev/null 2>&1'`. Matches
    the remote command it was asked to run rather than actually running
    it, since there is no real VM in a unit test."""
    if not reachable:
        return 'ssh() { return 255; }\n'  # ssh's own exit code for "unreachable"
    exit_code = "0" if has_opencode else "1"
    return f"""
ssh() {{
    case "$*" in
        *"command -v opencode"*) return {exit_code} ;;
        *) echo "unexpected ssh call: $*" >&2; return 1 ;;
    esac
}}
"""


def test_detect_opencode_capability_true_when_binary_present() -> None:
    result = _run(
        _fake_ssh(reachable=True, has_opencode=True)
        + 'detect_opencode_capability azure-epic1799 && echo DETECTED'
    )
    assert result.returncode == 0, result.stderr
    assert "DETECTED" in result.stdout


def test_detect_opencode_capability_false_when_binary_absent() -> None:
    result = _run(
        _fake_ssh(reachable=True, has_opencode=False)
        + 'detect_opencode_capability azure-epic1799 && echo DETECTED || echo NOT-DETECTED'
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "NOT-DETECTED"


# --- composition: what main() actually does with the two helpers -----------


def test_a_machine_whose_image_has_opencode_ends_up_advertising_the_capability() -> None:
    """#1799 acceptance: "a worker whose image contains opencode is
    registered advertising provider:opencode" — reproduces the exact
    composition step 2b/5 performs in main()."""
    result = _run(
        _fake_ssh(reachable=True, has_opencode=True)
        + 'CAPABILITIES="rust,python"\n'
        'if detect_opencode_capability azure-epic1799; then\n'
        '    CAPABILITIES="$(add_capability_if_missing "$CAPABILITIES" "provider:opencode")"\n'
        'fi\n'
        'echo "$CAPABILITIES"\n'
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "rust,python,provider:opencode"


def test_a_machine_whose_image_lacks_opencode_does_not_advertise_the_capability() -> None:
    """The flip side: an image without opencode must not get a
    `provider:opencode` claim it can't back — that is exactly the "declared
    but not installed" trap `coord.prereqs`/`coord doctor` exist to catch,
    and this is the registration-time half of avoiding it."""
    result = _run(
        _fake_ssh(reachable=True, has_opencode=False)
        + 'CAPABILITIES="rust,python"\n'
        'if detect_opencode_capability azure-epic1799; then\n'
        '    CAPABILITIES="$(add_capability_if_missing "$CAPABILITIES" "provider:opencode")"\n'
        'fi\n'
        'echo "$CAPABILITIES"\n'
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "rust,python"


def test_default_capabilities_no_longer_hardcodes_provider_opencode() -> None:
    """Static pin: the issue's complaint was a hardcoded default that
    drifted (correct before #1777, wrong once the image gained opencode).
    The fix is detection, not a different hardcoded default — assert the
    literal default assignment stays plain `rust,python`, not
    `rust,python,provider:opencode`."""
    text = SCRIPT.read_text()
    assert 'CAPABILITIES="rust,python"' in text
    assert 'CAPABILITIES="rust,python,provider:opencode"' not in text


def test_capability_detection_runs_before_registration() -> None:
    """Static pin: detect_opencode_capability must be called (and able to
    widen $CAPABILITIES) before the coordinator.yml registration step reads
    $CAPABILITIES — otherwise the detected capability never makes it into
    the generated entry."""
    text = SCRIPT.read_text()
    detect_call_idx = text.index('if detect_opencode_capability "$MACHINE"; then')
    register_idx = text.index('log "3/5  register in coordinator.yml')
    assert detect_call_idx < register_idx


# --- "verify before declaring ready" (#1799 acceptance) --------------------
#
# The issue's third bullet: epic-up.sh used to end with a "ready" banner for
# a machine that could not accept a single dispatch. The fix reuses
# `Machine.repo_path()` — the exact precondition `coord.dispatch.dispatch()`
# checks before refusing with "No repo_path configured" — inside the same
# remote Python validation snippet that already confirms the machine parsed
# (see the `coord.config.load` call in step 3/5). These are static pins
# (the snippet itself is exercised behaviourally in test_coordinator_machine
# via `coord.config.load` + `Machine.repo_path` directly); the point here is
# only that epic-up.sh actually WIRES the check in, in the right order,
# rather than silently registering and moving straight to "ready".


def test_repo_path_dispatchability_is_checked_before_the_ready_banner() -> None:
    text = SCRIPT.read_text()
    check_idx = text.index("missing = [r for r in repos if machine.repo_path(r) is None]")
    ready_idx = text.index('log "5/5  ready"')
    assert check_idx < ready_idx


def test_repo_path_dispatchability_check_fails_loudly_not_silently() -> None:
    """The check must actually be able to abort provisioning (`sys.exit`
    inside the heredoc, which the outer `set -euo pipefail` + no `|| true`
    propagates as a non-zero ssh exit) rather than just logging a warning
    that an operator can miss — that was exactly the #1799 failure mode:
    "ends with a ready banner ... told everything was fine"."""
    text = SCRIPT.read_text()
    heredoc_start = text.index('"$PYBIN" - "$TMP" "$MACHINE" "$REPOS" <<')
    body_start = text.index("\n", heredoc_start) + 1
    validation_block = text[body_start:text.index("\nPYEOF", body_start)]
    assert "missing = [r for r in repos if machine.repo_path(r) is None]" in validation_block
    assert "sys.exit(" in validation_block
    # No `|| true` / `2>/dev/null || true` softening the ssh call that runs
    # this block -- a failure here must propagate.
    ssh_call_line = next(
        line for line in text.splitlines()
        if line.strip().startswith('ssh "$DAEMON_HOST" bash -euo pipefail -s -- \\')
    )
    assert "|| true" not in ssh_call_line
